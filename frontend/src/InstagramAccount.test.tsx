import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import InstagramAccount from './InstagramAccount';
import { Api } from './api';
import type { InstagramAccountStatus } from './api';

function status(overrides: Partial<InstagramAccountStatus> = {}): InstagramAccountStatus {
  return { configured: false, state: 'unconfigured', message: '', session_generation: 0, request_id: 0, completed_request_id: 0, interaction_id: null, browser_available: false, updated_at: null, ...overrides };
}
function json(body: unknown) { return new Response(JSON.stringify(body), { headers: { 'Content-Type': 'application/json' } }); }
function apiMock(initial: InstagramAccountStatus) {
  let current = initial;
  const mock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    if (url === '/api/instagram-account/screen') return new Response(new Uint8Array([137, 80, 78, 71]), { headers: { 'Content-Type': 'image/png' } });
    if (init?.method === 'PUT') { current = status({ configured: true, state: 'logging_in', message: 'Anmeldung wird vorbereitet.', request_id: 1 }); return json(current); }
    if (init?.method === 'DELETE') { current = status(); return json(current); }
    return json(current);
  });
  vi.stubGlobal('fetch', mock);
  return mock;
}
let createUrl: ReturnType<typeof vi.fn>;
let revokeUrl: ReturnType<typeof vi.fn>;
beforeEach(() => {
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
