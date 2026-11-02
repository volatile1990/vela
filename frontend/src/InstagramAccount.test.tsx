import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import InstagramAccount from './InstagramAccount';
import { Api } from './api';
import type { InstagramAccountStatus } from './api';
import { readTotpQr } from './totpQr';

vi.mock('./totpQr', () => ({ readTotpQr: vi.fn() }));

function status(overrides: Partial<InstagramAccountStatus> = {}): InstagramAccountStatus {
  return { configured: false, state: 'unconfigured', message: '', session_generation: 0, request_id: 0, completed_request_id: 0, interaction_id: null, browser_available: false, updated_at: null, ...overrides };
}
function json(body: unknown) { return new Response(JSON.stringify(body), { headers: { 'Content-Type': 'application/json' } }); }
function apiMock(initial: InstagramAccountStatus) {
  let current = initial;
  const mock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    if (url === '/api/instagram-account/screen') return new Response(new Uint8Array([137, 80, 78, 71]), { headers: { 'Content-Type': 'image/png' } });
    if (url === '/api/instagram-account/totp' && init?.method === 'PUT') { current = { ...current, totp_configured: true }; return json(current); }
    if (url === '/api/instagram-account/totp' && init?.method === 'DELETE') { current = { ...current, totp_configured: false }; return json(current); }
    if (url === '/api/instagram-account' && init?.method === 'PUT') { current = status({ configured: true, state: 'logging_in', message: 'Anmeldung wird vorbereitet.', request_id: 1 }); return json(current); }
    if (url === '/api/instagram-account' && init?.method === 'DELETE') { current = status(); return json(current); }
    return json(current);
  });
  vi.stubGlobal('fetch', mock);
  return mock;
}
let createUrl: ReturnType<typeof vi.fn>;
let revokeUrl: ReturnType<typeof vi.fn>;
beforeEach(() => {
  vi.mocked(readTotpQr).mockReset();
  let sequence = 0;
  createUrl = vi.fn(() => `blob:instagram-test-${++sequence}`); revokeUrl = vi.fn();
  Object.defineProperty(URL, 'createObjectURL', { configurable: true, writable: true, value: createUrl });
  Object.defineProperty(URL, 'revokeObjectURL', { configurable: true, writable: true, value: revokeUrl });
});
afterEach(() => { vi.useRealTimers(); });

describe('Instagram-Zugang und Bestätigung', () => {
  it('ordnet ungültige Account-Eingaben einer Instagram-Meldung zu', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify({ detail: [] }), { status: 422, headers: { 'Content-Type': 'application/json' } })));
    await expect(new Api().saveInstagramAccount('', '')).rejects.toThrow('Bitte prüfe deine Instagram-Eingaben');
  });
  it('beginnt mit leeren Feldern, sendet frisch eingegebene Zugangsdaten und leert sie sofort', async () => {
    const fetchMock = apiMock(status());
    const user = userEvent.setup();
    render(<InstagramAccount api={new Api()} />);
    const username = await screen.findByLabelText('Instagram-Benutzername');
    const password = screen.getByLabelText('Instagram-Passwort');
    expect(username).toHaveValue(''); expect(password).toHaveValue('');
    await user.type(username, 'synthetic-user'); await user.type(password, 'synthetic-password');
    await user.click(screen.getByRole('button', { name: 'Speichern und anmelden' }));
    await screen.findByText('Anmeldung wird vorbereitet.');
    expect(fetchMock).toHaveBeenCalledWith('/api/instagram-account', expect.objectContaining({ method: 'PUT', body: JSON.stringify({ username: 'synthetic-user', password: 'synthetic-password' }) }));
    expect(screen.getByLabelText('Instagram-Benutzername')).toHaveValue('');
    expect(screen.getByLabelText('Instagram-Passwort')).toHaveValue('');
    expect(screen.queryByText('synthetic-user')).not.toBeInTheDocument();
    expect(screen.queryByText('synthetic-password')).not.toBeInTheDocument();
  });

  it('liest keine Zugangsdaten aus einer Statusantwort in die Eingabefelder zurück', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => json({ ...status({ configured: true, state: 'ready' }), username: 'never-display-this-user', password: 'never-display-this-password' })));
    render(<InstagramAccount api={new Api()} />);
    await screen.findByText('Verbunden', { selector: '.ig-account-state' });
    expect(screen.getByLabelText('Instagram-Benutzername')).toHaveValue('');
    expect(screen.getByLabelText('Instagram-Passwort')).toHaveValue('');
    expect(screen.queryByText(/never-display/)).not.toBeInTheDocument();
  });

  it('behandelt einen fehlenden Authenticator-Status als nicht hinterlegt und benötigt zuerst einen Zugang', async () => {
    const fetchMock = apiMock(status());
    render(<InstagramAccount api={new Api()} />);
    const secret = await screen.findByLabelText('Instagram-Einrichtungsschlüssel');
    expect(secret).toBeDisabled();
    expect(screen.getByLabelText('Authenticator-Schlüssel: Nicht hinterlegt')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Schlüssel speichern' })).toBeDisabled();
    expect(screen.getByText(/keinen sechsstelligen Bestätigungscode/)).toBeInTheDocument();
    expect(fetchMock.mock.calls.some(([, init]) => init?.method === 'PUT')).toBe(false);
  });

  it('sendet den Einrichtungsschlüssel getrennt und leert ihn bereits während des offenen Requests', async () => {
    const initial = status({ configured: true, state: 'ready', request_id: 3, completed_request_id: 3, session_generation: 2 });
    let finish!: (response: Response) => void;
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      if (String(input) === '/api/instagram-account/totp' && init?.method === 'PUT') return new Promise<Response>(resolve => { finish = resolve; });
      return json(initial);
    });
    vi.stubGlobal('fetch', fetchMock);
    const user = userEvent.setup();
    render(<InstagramAccount api={new Api()} />);
    const secret = await screen.findByLabelText('Instagram-Einrichtungsschlüssel');
    expect(secret).toHaveAttribute('type', 'password');
    expect(secret).toHaveAttribute('autocomplete', 'off');
    expect(secret).toHaveAttribute('maxlength', '4096');
    const syntheticSecret = 'JBSWY3DPEHPK3PXP';
    await user.type(secret, syntheticSecret);
    await user.click(screen.getByRole('button', { name: 'Schlüssel speichern' }));
    expect(secret).toHaveValue(''); expect(secret).toBeDisabled();
    expect(fetchMock).toHaveBeenCalledWith('/api/instagram-account/totp', expect.objectContaining({ method: 'PUT', body: JSON.stringify({ secret: syntheticSecret }) }));
    expect(fetchMock.mock.calls.some(([url, init]) => String(url).endsWith('/login') || (String(url) === '/api/instagram-account' && init?.method !== 'GET'))).toBe(false);
    await act(async () => { finish(json({ ...initial, totp_configured: true, secret: syntheticSecret, totp_secret: syntheticSecret })); });
    await screen.findByLabelText('Authenticator-Schlüssel: Hinterlegt');
    expect(secret).toHaveValue('');
    expect(screen.queryByText(syntheticSecret)).not.toBeInTheDocument();
    expect(screen.getByText(/keine neue Anmeldung gestartet/)).toBeInTheDocument();
    expect(screen.getByText('Verbunden', { selector: '.ig-account-state' })).toBeInTheDocument();
  });

  it('unterstützt einen otpauth-Link und zeigt auch bei einem Fehler keine geheimen Serverdetails', async () => {
    const initial = status({ configured: true, state: 'idle' });
    const syntheticUri = 'otpauth://totp/Instagram:synthetic?secret=JBSWY3DPEHPK3PXP&issuer=Instagram';
    const fetchMock = vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => init?.method === 'PUT'
      ? new Response(JSON.stringify({ detail: syntheticUri }), { status: 422, headers: { 'Content-Type': 'application/json' } })
      : json({ ...initial, secret: syntheticUri, totp_secret: syntheticUri }));
    vi.stubGlobal('fetch', fetchMock);
    const user = userEvent.setup();
    render(<InstagramAccount api={new Api()} />);
    const secret = await screen.findByLabelText('Instagram-Einrichtungsschlüssel');
    expect(secret).toHaveValue('');
    await user.type(secret, syntheticUri);
    await user.click(screen.getByRole('button', { name: 'Schlüssel speichern' }));
    await screen.findByRole('alert');
    expect(secret).toHaveValue('');
    expect(screen.getByRole('alert')).toHaveTextContent('Base32-Zeichenfolge oder den otpauth-Link');
    expect(screen.queryByText(syntheticUri)).not.toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledWith('/api/instagram-account/totp', expect.objectContaining({ method: 'PUT', body: JSON.stringify({ secret: syntheticUri }) }));
  });

  it('erklärt den Einrichtungsschlüssel und sendet keinen sechsstelligen Code als Schlüssel', async () => {
    const fetchMock = apiMock(status({ configured: true, state: 'idle' }));
    const user = userEvent.setup();
    render(<InstagramAccount api={new Api()} />);
    const secret = await screen.findByLabelText('Instagram-Einrichtungsschlüssel');
    await user.type(secret, '123456');
    await user.click(screen.getByRole('button', { name: 'Schlüssel speichern' }));
    expect(screen.getByRole('alert')).toHaveTextContent('keinen sechsstelligen Bestätigungscode');
    expect(fetchMock.mock.calls.some(([, init]) => init?.method === 'PUT')).toBe(false);
    await user.click(screen.getByText('Welcher Schlüssel ist gemeint?'));
    expect(screen.getByText(/Ein aktueller Code aus Google Authenticator, einer E-Mail oder einer Nachricht ist dafür nicht geeignet/)).toBeVisible();
    expect(screen.getByText(/Teile ihn nicht im Chat/)).toBeVisible();
  });

  it('entfernt ausschließlich den Authenticator-Schlüssel und erhält Zugang sowie Sitzung', async () => {
    const fetchMock = apiMock(status({ configured: true, state: 'ready', totp_configured: true, request_id: 3, completed_request_id: 3, session_generation: 2 }));
    const user = userEvent.setup();
    const sessionChange = vi.fn();
    render(<InstagramAccount api={new Api()} onSessionChange={sessionChange} />);
    await user.click(await screen.findByRole('button', { name: 'Schlüssel entfernen' }));
    await screen.findByLabelText('Authenticator-Schlüssel: Nicht hinterlegt');
    expect(fetchMock).toHaveBeenCalledWith('/api/instagram-account/totp', expect.objectContaining({ method: 'DELETE' }));
    expect(fetchMock.mock.calls.some(([url, init]) => String(url).endsWith('/login') || (String(url) === '/api/instagram-account' && init?.method !== 'GET'))).toBe(false);
    expect(screen.getByRole('button', { name: 'Zugang entfernen' })).toBeInTheDocument();
    expect(screen.getByText('Verbunden', { selector: '.ig-account-state' })).toBeInTheDocument();
    expect(screen.getByText(/Zugang und Sitzung bleiben bestehen/)).toBeInTheDocument();
    expect(sessionChange).not.toHaveBeenCalled();
  });

  it('verwirft einen ungespeicherten Einrichtungsschlüssel beim Entfernen des Zugangs', async () => {
    apiMock(status({ configured: true, state: 'idle' }));
    const user = userEvent.setup();
    render(<InstagramAccount api={new Api()} />);
    const secret = await screen.findByLabelText('Instagram-Einrichtungsschlüssel');
    await user.type(secret, 'JBSWY3DPEHPK3PXP');
    await user.click(screen.getByRole('button', { name: 'Zugang entfernen' }));
    await screen.findByText('Nicht verbunden', { selector: '.ig-account-state' });
    expect(secret).toHaveValue(''); expect(secret).toBeDisabled();
  });

  it('leert die Bildauswahl sofort und sendet ausschließlich den erfolgreich gelesenen QR-Inhalt', async () => {
    const fetchMock = apiMock(status({ configured: true, state: 'idle' }));
    let finish!: (content: string) => void;
    vi.mocked(readTotpQr).mockImplementationOnce(() => new Promise(resolve => { finish = resolve; }));
    render(<InstagramAccount api={new Api()} />);
    const input = await screen.findByLabelText('QR-Bild für Instagram auswählen');
    Object.defineProperty(input, 'value', { configurable: true, writable: true, value: 'C:\\fakepath\\private-qr.png' });
    const file = new File(['synthetic'], 'private-qr.png', { type: 'image/png' });
    fireEvent.change(input, { target: { files: [file] } });
    expect(input).toHaveValue('');
    expect(readTotpQr).toHaveBeenCalledWith(file);
    expect(fetchMock.mock.calls.some(([, init]) => init?.method === 'PUT')).toBe(false);
    const syntheticUri = 'otpauth-migration://offline?data=synthetic';
    await act(async () => { finish(syntheticUri); });
    await screen.findByLabelText('Authenticator-Schlüssel: Hinterlegt');
    expect(fetchMock).toHaveBeenCalledWith('/api/instagram-account/totp', expect.objectContaining({ method: 'PUT', body: JSON.stringify({ secret: syntheticUri }) }));
    expect(fetchMock.mock.calls.some(([url]) => String(url).endsWith('/login'))).toBe(false);
    expect(screen.queryByText(/private-qr|syntheticUri/)).not.toBeInTheDocument();
    expect(screen.queryByRole('img')).not.toBeInTheDocument();
  });

  it('sendet bei unlesbarem QR-Bild nichts und zeigt keine Dateinamen oder Decoderdetails', async () => {
    const fetchMock = apiMock(status({ configured: true, state: 'idle' }));
    vi.mocked(readTotpQr).mockRejectedValueOnce(new Error('private-qr.png synthetic-secret'));
    render(<InstagramAccount api={new Api()} />);
    const input = await screen.findByLabelText('QR-Bild für Instagram auswählen');
    fireEvent.change(input, { target: { files: [new File(['synthetic'], 'private-qr.png', { type: 'image/png' })] } });
    await screen.findByRole('alert');
    expect(screen.getByRole('alert')).toHaveTextContent('Das QR-Bild konnte nicht übernommen werden');
    expect(input).toHaveValue('');
    expect(screen.queryByText(/private-qr|synthetic-secret/)).not.toBeInTheDocument();
    expect(fetchMock.mock.calls.some(([, init]) => init?.method === 'PUT')).toBe(false);
  });

  it('sendet nach dem Verlassen keinen erst später gelesenen QR-Inhalt mehr', async () => {
    const fetchMock = apiMock(status({ configured: true, state: 'idle' }));
    let finish!: (content: string) => void;
    vi.mocked(readTotpQr).mockImplementationOnce(() => new Promise(resolve => { finish = resolve; }));
    const rendered = render(<InstagramAccount api={new Api()} />);
    const input = await screen.findByLabelText('QR-Bild für Instagram auswählen');
    fireEvent.change(input, { target: { files: [new File(['synthetic'], 'qr.png', { type: 'image/png' })] } });
    rendered.unmount();
    await act(async () => { finish('otpauth://totp/Instagram:synthetic?secret=JBSWY3DPEHPK3PXP'); });
    expect(fetchMock.mock.calls.some(([, init]) => init?.method === 'PUT')).toBe(false);
  });

  it('lädt das Livebild erst nach Öffnen und sendet normalisierte Klicks sowie Texteingaben', async () => {
    const fetchMock = apiMock(status({ configured: true, state: 'needs_attention', browser_available: true, interaction_id: 'interaction-1' }));
    const user = userEvent.setup();
    render(<InstagramAccount api={new Api()} />);
    await screen.findByText('Bestätigung erforderlich');
    expect(fetchMock.mock.calls.some(([url]) => String(url).endsWith('/screen'))).toBe(false);
    await user.click(screen.getByRole('button', { name: 'Bestätigung öffnen' }));
    const image = await screen.findByRole('img', { name: 'Aktuelles Livebild des Instagram-Browsers' });
    Object.defineProperty(image, 'naturalWidth', { configurable: true, value: 1000 });
    Object.defineProperty(image, 'naturalHeight', { configurable: true, value: 760 });
    vi.spyOn(image, 'getBoundingClientRect').mockReturnValue({ x: 10, y: 20, left: 10, top: 20, width: 500, height: 380, right: 510, bottom: 400, toJSON() { return {}; } });
    fireEvent.load(image);
    fireEvent.click(screen.getByRole('button', { name: /Im Instagram-Browser klicken/ }), { detail: 1, clientX: 260, clientY: 210 });
    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith('/api/instagram-account/action', expect.objectContaining({ body: JSON.stringify({ kind: 'click', x: 0.5, y: 0.5, interaction_id: 'interaction-1' }) })));
    await user.type(screen.getByLabelText('Text oder Bestätigungscode'), '123456');
    await user.click(screen.getByRole('button', { name: 'Text eingeben' }));
    expect(fetchMock).toHaveBeenCalledWith('/api/instagram-account/action', expect.objectContaining({ body: JSON.stringify({ kind: 'text', text: '123456', interaction_id: 'interaction-1' }) }));
    expect(screen.getByLabelText('Text oder Bestätigungscode')).toHaveValue('');
    expect(fetchMock).toHaveBeenCalledWith('/api/instagram-account/screen', expect.objectContaining({ cache: 'no-store' }));
  });

  it('gibt ersetzte Livebilder frei und stoppt alle Abfragen nach dem Verlassen', async () => {
    const fetchMock = apiMock(status({ configured: true, state: 'needs_attention', browser_available: true, interaction_id: 'interaction-1' }));
    vi.useFakeTimers();
    const rendered = render(<InstagramAccount api={new Api()} />);
    await act(async () => { await vi.advanceTimersByTimeAsync(0); });
    fireEvent.click(screen.getByRole('button', { name: 'Bestätigung öffnen' }));
    await act(async () => { await vi.advanceTimersByTimeAsync(0); });
    expect(screen.getByRole('img', { name: 'Aktuelles Livebild des Instagram-Browsers' })).toBeInTheDocument();
    expect(createUrl).toHaveBeenCalledTimes(1);
    await act(async () => { await vi.advanceTimersByTimeAsync(2100); });
    expect(createUrl).toHaveBeenCalledTimes(2);
    expect(revokeUrl).toHaveBeenCalledWith('blob:instagram-test-1');
    rendered.unmount();
    expect(revokeUrl).toHaveBeenCalledWith('blob:instagram-test-2');
    const calls = fetchMock.mock.calls.length;
    await act(async () => { await vi.advanceTimersByTimeAsync(10000); });
    expect(fetchMock).toHaveBeenCalledTimes(calls);
  });

  it('beendet das Laden von Livebildern beim Schließen der Bestätigung', async () => {
    const fetchMock = apiMock(status({ configured: true, state: 'needs_attention', browser_available: true, interaction_id: 'interaction-1' }));
    vi.useFakeTimers();
    const rendered = render(<InstagramAccount api={new Api()} />);
    await act(async () => { await vi.advanceTimersByTimeAsync(0); });
    fireEvent.click(screen.getByRole('button', { name: 'Bestätigung öffnen' }));
    await act(async () => { await vi.advanceTimersByTimeAsync(0); });
    expect(screen.getByRole('img', { name: 'Aktuelles Livebild des Instagram-Browsers' })).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Bestätigung schließen' }));
    expect(screen.queryByRole('img')).not.toBeInTheDocument();
    expect(revokeUrl).toHaveBeenCalledWith('blob:instagram-test-1');
    const images = fetchMock.mock.calls.filter(([url]) => String(url).endsWith('/screen')).length;
    await act(async () => { await vi.advanceTimersByTimeAsync(4100); });
    expect(fetchMock.mock.calls.filter(([url]) => String(url).endsWith('/screen'))).toHaveLength(images);
    rendered.unmount();
  });
});
