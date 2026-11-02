import { beforeEach, describe, expect, it, vi } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import App from './App';
import { Api, validMediaUrl } from './api';
import type { Job, ServerInfo } from './api';

const serverInfo: ServerInfo = { version: '1.0.0' };
function job(overrides: Partial<Job> = {}): Job {
  return {
    id: 'job-1', url: 'https://example.com/video', kind: 'download', profile: 'original', max_height: 1080,
    playlist: false, playlist_items: null, status: 'queued', phase: 'queued', progress: null, downloaded_bytes: null,
    total_bytes: null, speed: null, eta: null, title: null, extractor: null, metadata: {}, error: null,
    error_code: null, cancel_requested: false, attempt: 0, created_at: new Date().toISOString(), updated_at: new Date().toISOString(),
    finished_at: null, artifacts: [], ...overrides,
  };
}
function json(body: unknown, status = 200): Response { return new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } }); }
const overview = (workerStatus = 'running') => ({ version: '1.0.0', counts: { queued: 0, active: 0, completed: 0, failed: 0 }, worker: { heartbeat_at: new Date().toISOString(), status: workerStatus, storage: { available: true, free_bytes: 1024 ** 4, total_bytes: 2 * 1024 ** 4, label: 'Vela' } } });

function backend(existingJobs: Job[] = [], workerStatus = 'running') {
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    if (url === '/api/session') return json(serverInfo);
    if (url === '/api/overview') return json(overview(workerStatus));
    if (url === '/api/source-sessions') return json({ items: [] });
    if (url === '/api/instagram-account') return json({ configured: false, state: 'unconfigured', message: '', session_generation: 0, request_id: 0, completed_request_id: 0, interaction_id: null, browser_available: false, updated_at: null });
    if (url.startsWith('/api/jobs?')) return json({ items: existingJobs, total: existingJobs.length });
    if (url === '/api/jobs' && init?.method === 'POST') return json(job(JSON.parse(String(init.body)) as Partial<Job>), 202);
    if (url === '/api/previews') return json(job({ kind: 'probe', status: 'completed', title: 'Meine Aufnahme', metadata: { title: 'Meine Aufnahme', duration: 125, thumbnail: 'https://external.example/private.jpg' } }), 202);
    if (url.endsWith('/cancel')) return json(job({ cancel_requested: true, status: 'downloading' }));
    if (url.endsWith('/retry')) return json(job());
    return json({ detail: `Unerwarteter Test-Endpunkt ${url}` }, 404);
  });
  vi.stubGlobal('fetch', fetchMock);
  return fetchMock;
}

beforeEach(() => { vi.restoreAllMocks(); window.history.replaceState(null, '', '/'); });

describe('Medienlinks und API-Anfragen', () => {
  it('lehnt aktive Schemata und Zugangsdaten in Medienlinks ab', () => {
    expect(validMediaUrl('https://example.com/a?v=1')).toBe(true);
    expect(validMediaUrl('javascript:alert(1)')).toBe(false);
    expect(validMediaUrl('file:///etc/passwd')).toBe(false);
    expect(validMediaUrl('https://secret:password@example.com')).toBe(false);
    expect(validMediaUrl('kein link')).toBe(false);
  });

  it('sendet den Auftrag mit Idempotenzschlüssel ohne Zugangstoken', async () => {
    const fetchMock = backend();
    await new Api().create({ url: 'https://example.com/video', profile: 'audio', max_height: null, playlist: false, playlist_items: null }, 'request-123');
    expect(fetchMock).toHaveBeenCalledWith('/api/jobs', expect.objectContaining({ method: 'POST', credentials: 'same-origin', headers: { Accept: 'application/json', 'Content-Type': 'application/json', 'Idempotency-Key': 'request-123' } }));
  });

  it('übersetzt eine abgewiesene Origin in eine verständliche deutsche Meldung', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => json({ detail: 'Invalid origin' }, 403)));
    await expect(new Api().cancel('job-1')).rejects.toThrow('Bitte lade die Seite neu.');
  });
});

describe('Vela-Arbeitsabläufe', () => {
  it('zeigt einen gestoppten Worker auch bei frischem Heartbeat nicht als bereit', async () => {
    backend([], 'stopped');
    render(<App />);
    expect(await screen.findByText('Worker nicht bereit')).toBeInTheDocument();
    expect(screen.queryByText('Bereit für Downloads')).not.toBeInTheDocument();
  });

  it('normalisiert Leerzeichen in der Playlist-Auswahl vor dem Versand', async () => {
    const fetchMock = backend();
    const user = userEvent.setup();
    render(<App />);
    await user.type(await screen.findByLabelText('Medienlink'), 'https://example.com/playlist');
    await user.click(screen.getByRole('checkbox', { name: 'Playlist herunterladen' }));
    await user.type(screen.getByLabelText(/Playlist-Einträge/), '1-5, 8');
    await user.click(screen.getByRole('button', { name: 'Download starten' }));
    await screen.findByText('Hinzugefügt. Dein Download steht in der Warteschlange.');
    const call = fetchMock.mock.calls.find(([input, init]) => input === '/api/jobs' && init?.method === 'POST');
    expect(JSON.parse(String(call?.[1]?.body)).playlist_items).toBe('1-5,8');
  });
  it('öffnet direkt die Warteschlange ohne Kontooberfläche', async () => {
    backend();
    render(<App />);
    expect(await screen.findByRole('heading', { name: 'Was möchtest du speichern?' })).toBeInTheDocument();
    expect(await screen.findByText('Alles ist erledigt.')).toBeInTheDocument();
    expect(screen.queryByLabelText('Passwort')).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /Anmelden|Abmelden/ })).not.toBeInTheDocument();
  });

  it('bleibt bei einer abgewiesenen Aktion auf dem Dashboard', async () => {
    const fetchMock = backend();
    const normal = fetchMock.getMockImplementation()!;
    fetchMock.mockImplementation(async (input, init) => String(input) === '/api/jobs' && init?.method === 'POST' ? json({}, 401) : normal(input, init));
    const user = userEvent.setup();
    render(<App />);
    await user.type(await screen.findByLabelText('Medienlink'), 'https://example.com/video');
    await user.click(screen.getByRole('button', { name: 'Download starten' }));
    expect(await screen.findByText('Die Anfrage ist nicht erlaubt. Bitte lade die Seite neu.')).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: 'Was möchtest du speichern?' })).toBeInTheDocument();
    expect(screen.queryByLabelText('Passwort')).not.toBeInTheDocument();
  });

  it('erstellt einen Audioauftrag ohne Auflösungsgrenze und zeigt dessen Wartestatus', async () => {
    const fetchMock = backend();
    const user = userEvent.setup();
    render(<App />);
    await user.type(await screen.findByLabelText('Medienlink'), 'https://example.com/video');
    await user.click(screen.getByRole('radio', { name: /Nur Audio/ }));
    await user.click(screen.getByRole('button', { name: 'Download starten' }));
    expect(await screen.findByText('Hinzugefügt. Dein Download steht in der Warteschlange.')).toBeInTheDocument();
    expect(await screen.findByText('Wartet')).toBeInTheDocument();
    const createCall = fetchMock.mock.calls.find(([input, init]) => input === '/api/jobs' && init?.method === 'POST');
    expect(JSON.parse(String(createCall?.[1]?.body))).toEqual({ url: 'https://example.com/video', profile: 'audio', max_height: null, playlist: false, playlist_items: null });
    expect(screen.getByLabelText('Medienlink')).toHaveValue('');
  });

  it('lädt eine Metadatenvorschau ohne externe Thumbnail-Anfragen', async () => {
    const fetchMock = backend();
    const user = userEvent.setup();
    render(<App />);
    await user.type(await screen.findByLabelText('Medienlink'), 'https://example.com/video');
    await user.click(screen.getByRole('button', { name: 'Vorschau' }));
    expect(await screen.findByRole('heading', { name: 'Meine Aufnahme' })).toBeInTheDocument();
    expect(screen.getByText('2:05', { exact: false })).toBeInTheDocument();
    expect(screen.queryByRole('img')).not.toBeInTheDocument();
    expect(fetchMock.mock.calls.some(([input]) => String(input).startsWith('https://external.'))).toBe(false);
  });

  it('sendet den Abbruch für den richtigen laufenden Auftrag', async () => {
    const fetchMock = backend([job({ status: 'downloading', title: 'Eigene Aufnahme', progress: 42, speed: 500000, total_bytes: 1000000, downloaded_bytes: 420000 })]);
    const user = userEvent.setup();
    render(<App />);
    await user.click(await screen.findByRole('button', { name: 'Download abbrechen: Eigene Aufnahme' }));
    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith('/api/jobs/job-1/cancel', expect.objectContaining({ method: 'POST' })));
  });

  it('aktualisiert auch einen älteren bereits geladenen aktiven Auftrag', async () => {
    const recent = job({ id: 'recent', title: 'Neuester Auftrag', status: 'completed' });
    const older = job({ id: 'older', title: 'Älterer Download', status: 'downloading', progress: 20 });
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url === '/api/session') return json(serverInfo);
      if (url === '/api/overview') return json(overview());
      if (url === '/api/jobs?limit=100&offset=0') return json({ items: [recent], total: 2 });
      if (url === '/api/jobs?limit=100&offset=1') return json({ items: [older], total: 2 });
      if (url === '/api/jobs/older') return json({ ...older, progress: 75, updated_at: new Date(Date.now() + 1000).toISOString() });
      return json({}, 404);
    });
    vi.stubGlobal('fetch', fetchMock);
    const user = userEvent.setup();
    render(<App />);
    await user.click(await screen.findByRole('button', { name: 'Verlauf' }));
    await user.click(await screen.findByRole('button', { name: 'Ältere Aufträge laden' }));
    await user.click(screen.getByRole('button', { name: /Downloads/ }));
    await screen.findByRole('heading', { name: 'Älterer Download' });
    window.dispatchEvent(new Event('focus'));
    await waitFor(() => expect(screen.getByRole('progressbar', { name: 'Fortschritt für Älterer Download' })).toHaveAttribute('aria-valuenow', '75'));
    expect(fetchMock).toHaveBeenCalledWith('/api/jobs/older', expect.anything());
  });

  it('zeigt einen fehlgeschlagenen Auftrag im Verlauf und ermöglicht Wiederholen', async () => {
    const fetchMock = backend([job({ status: 'failed', title: 'Eigene Aufnahme', error: 'Quelle vorübergehend nicht erreichbar.' })]);
    const user = userEvent.setup();
    render(<App />);
    await screen.findByRole('heading', { name: 'Was möchtest du speichern?' });
    await user.click(screen.getByRole('button', { name: 'Verlauf' }));
    await user.click(await screen.findByRole('button', { name: 'Download wiederholen: Eigene Aufnahme' }));
    expect(fetchMock).toHaveBeenCalledWith('/api/jobs/job-1/retry', expect.objectContaining({ method: 'POST' }));
  });

  it('führt bei einer Zielgruppenbeschränkung direkt zu den passenden Quellsitzungen', async () => {
    backend([job({ status: 'failed', url: 'https://www.instagram.com/reel/example', title: 'Eigene Aufnahme', error: 'Die Quelle verlangt eine passende Sitzung.', error_code: 'audience_restricted' })]);
    const user = userEvent.setup();
    render(<App />);
    await user.click(await screen.findByRole('button', { name: 'Instagram bestätigen' }));
    expect(await screen.findByRole('heading', { name: 'Sitzungen für Quellen' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /Instagram/ })).toHaveAttribute('aria-pressed', 'true');
    expect(screen.queryByLabelText('Passwort')).not.toBeInTheDocument();
  });

  it('zeigt einen wartenden Instagram-Auftrag mit direkter Bestätigung an', async () => {
    backend([job({ status: 'waiting_auth', url: 'https://www.instagram.com/reel/example', title: 'Eigene Aufnahme' })]);
    const user = userEvent.setup();
    render(<App />);
    expect(await screen.findByText('Wartet auf Instagram', { selector: '.status-label' })).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: 'Instagram bestätigen' }));
    expect(await screen.findByRole('heading', { name: 'Instagram verbinden' })).toBeInTheDocument();
  });
});
