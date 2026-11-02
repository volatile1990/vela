import { describe, expect, it, vi } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import SourceSessions from './SourceSessions';
import { Api, sourceSites, sourceSiteForUrl } from './api';
import type { SourceSession } from './api';

function json(body: unknown, status = 200) { return new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } }); }
function emptySessions(): SourceSession[] { return sourceSites.map(item => ({ ...item, configured: false, cookie_count: 0, expires_at: null, updated_at: null, state: 'missing' })); }

describe('Sitzungen für Quellen', () => {
  it('überträgt nur die bewusst gewählte Datei als unveränderten Text und zeigt keine Werte oder Dateinamen', async () => {
    let items = emptySessions();
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      if (String(input) === '/api/source-sessions/instagram' && init?.method === 'PUT') {
        items = items.map(item => item.site === 'instagram' ? { ...item, configured: true, cookie_count: 1, updated_at: '2026-11-02T12:00:00Z', state: 'configured' } : item);
        return json(items[0]);
      }
      return json({ items });
    });
    vi.stubGlobal('fetch', fetchMock);
    const user = userEvent.setup();
    render(<SourceSessions api={new Api()} />);
    await screen.findByText('Nicht hinterlegt', { selector: '.source-state' });
    const syntheticContent = '# Netscape HTTP Cookie File\n.instagram.com\tTRUE\t/\tTRUE\t0\tsessionid\tsynthetic-test-value\n';
    const file = new File([syntheticContent], 'personal-identity.txt', { type: 'text/plain' });
    Object.defineProperty(file, 'text', { value: async () => syntheticContent });
    await user.upload(screen.getByLabelText('cookies.txt für Instagram auswählen'), file);
    expect(screen.queryByText('personal-identity.txt')).not.toBeInTheDocument();
    expect(screen.queryByText(/synthetic-test-value/)).not.toBeInTheDocument();
    expect(fetchMock.mock.calls.some(([, init]) => init?.method === 'PUT')).toBe(false);
    await user.click(screen.getByRole('button', { name: 'Sitzung hinterlegen' }));
    await screen.findByText('Die Sitzung wurde hinterlegt. Du kannst den Download jetzt erneut versuchen.');
    expect(fetchMock).toHaveBeenCalledWith('/api/source-sessions/instagram', expect.objectContaining({ method: 'PUT', body: syntheticContent, headers: { Accept: 'application/json', 'Content-Type': 'text/plain;charset=UTF-8' } }));
    expect(screen.getByText('1 Cookie')).toBeInTheDocument();
    expect(screen.queryByText(/synthetic-test-value/)).not.toBeInTheDocument();
  });

  it('weist zu große Dateien vor der Übertragung zurück', async () => {
    const fetchMock = vi.fn(async () => json({ items: emptySessions() }));
    vi.stubGlobal('fetch', fetchMock);
    const user = userEvent.setup();
    render(<SourceSessions api={new Api()} />);
    await screen.findByText('Nicht hinterlegt', { selector: '.source-state' });
    await user.upload(screen.getByLabelText('cookies.txt für Instagram auswählen'), new File([new Uint8Array(256 * 1024 + 1)], 'cookies.txt', { type: 'text/plain' }));
    expect(screen.getByRole('alert')).toHaveTextContent('höchstens 256 KB');
    expect(screen.getByRole('button', { name: 'Sitzung hinterlegen' })).toBeDisabled();
    expect(fetchMock).not.toHaveBeenCalledWith('/api/source-sessions/instagram', expect.objectContaining({ method: 'PUT' }));
  });

  it('entfernt eine abgelaufene Sitzung ohne Cookie-Inhalte zu lesen', async () => {
    let items = emptySessions().map(item => item.site === 'instagram' ? { ...item, state: 'expired' as const, updated_at: '2026-10-01T12:00:00Z', expires_at: '2026-10-31T12:00:00Z' } : item);
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      if (String(input) === '/api/source-sessions/instagram' && init?.method === 'DELETE') { items = emptySessions(); return json({ ok: true }); }
      return json({ items });
    });
    vi.stubGlobal('fetch', fetchMock);
    const user = userEvent.setup();
    render(<SourceSessions api={new Api()} />);
    expect(await screen.findByText('Abgelaufen', { selector: '.source-state' })).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: 'Entfernen' }));
    await screen.findByText('Die Sitzung wurde entfernt. Neue Aufträge verwenden sie nicht mehr.');
    expect(fetchMock).toHaveBeenCalledWith('/api/source-sessions/instagram', expect.objectContaining({ method: 'DELETE' }));
    expect(screen.getByText('Nicht hinterlegt', { selector: '.source-state' })).toBeInTheDocument();
  });

  it('ordnet ausschließlich passende Hostnamen einer festen Quelle zu', () => {
    expect(sourceSiteForUrl('https://www.instagram.com/reel/example')).toBe('instagram');
    expect(sourceSiteForUrl('https://youtu.be/example')).toBe('youtube');
    expect(sourceSiteForUrl('https://instagram.com.evil.example/video')).toBeNull();
    expect(sourceSiteForUrl('https://example.com/video')).toBeNull();
  });

  it('verwirft eine gewählte Datei beim Wechsel der Quelle', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => json({ items: emptySessions() })));
    const user = userEvent.setup();
    render(<SourceSessions api={new Api()} />);
    await screen.findByText('Nicht hinterlegt', { selector: '.source-state' });
    await user.upload(screen.getByLabelText('cookies.txt für Instagram auswählen'), new File(['synthetic'], 'cookies.txt', { type: 'text/plain' }));
    expect(screen.getByRole('button', { name: 'Sitzung hinterlegen' })).toBeEnabled();
    await user.click(screen.getByRole('button', { name: /YouTube/ }));
    await waitFor(() => expect(screen.getByRole('button', { name: 'Sitzung hinterlegen' })).toBeDisabled());
  });
});
