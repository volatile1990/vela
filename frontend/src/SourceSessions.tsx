import { useCallback, useEffect, useRef, useState } from 'react';
import type { ChangeEvent } from 'react';
import { Api, formatBytes, sourceSites } from './api';
import type { SourceSession, SourceSite } from './api';
import { Icon } from './icons';
import InstagramAccount from './InstagramAccount';

const MAX_SIZE = 256 * 1024;
const stateLabels = { missing: 'Nicht hinterlegt', configured: 'Hinterlegt', expired: 'Abgelaufen', invalid: 'Erneuern erforderlich' };
function dateLabel(value: string | null | undefined): string {
  if (!value || !Number.isFinite(Date.parse(value))) return 'Kein festes Ablaufdatum';
  return new Date(value).toLocaleString('de-DE', { day: '2-digit', month: '2-digit', year: 'numeric', hour: '2-digit', minute: '2-digit' });
}

export default function SourceSessions({ api, initialSite = 'instagram', focusOnOpen = false }: { api: Api; initialSite?: SourceSite; focusOnOpen?: boolean }) {
  const [items, setItems] = useState<SourceSession[]>([]);
  const [selectedSite, setSelectedSite] = useState<SourceSite>(initialSite);
  const [file, setFile] = useState<File | null>(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [message, setMessage] = useState('');
  const fileInput = useRef<HTMLInputElement>(null);
  const heading = useRef<HTMLHeadingElement>(null);
  const selectedSiteRef = useRef(selectedSite);
  const load = useCallback(async () => {
    setLoading(true); setError('');
    try { const result = await api.sourceSessions(); setItems(result.items); }
    catch { setError('Die Sitzungen konnten nicht geladen werden. Bitte versuche es erneut.'); }
    finally { setLoading(false); }
  }, [api]);
  useEffect(() => { void load(); }, [load]);
  useEffect(() => {
    setSelectedSite(initialSite); setFile(null); setError(''); setMessage('');
    if (fileInput.current) fileInput.current.value = '';
    if (focusOnOpen) heading.current?.focus();
  }, [initialSite, focusOnOpen]);
  useEffect(() => { selectedSiteRef.current = selectedSite; }, [selectedSite]);
  const selected = items.find(item => item.site === selectedSite);
  const label = sourceSites.find(source => source.site === selectedSite)!.label;
  const state = selected?.state ?? 'missing';
  const hasSession = Boolean(selected?.configured || selected?.updated_at || state === 'expired' || state === 'invalid');

  function chooseSite(site: SourceSite) {
    if (busy) return;
    setSelectedSite(site); setFile(null); setError(''); setMessage('');
    if (fileInput.current) fileInput.current.value = '';
  }
  function chooseFile(event: ChangeEvent<HTMLInputElement>) {
    setError(''); setMessage(''); setFile(null);
    const candidate = event.target.files?.[0];
    if (!candidate) return;
    if (!candidate.name.toLowerCase().endsWith('.txt')) { setError('Wähle eine cookies.txt im Netscape-Format.'); event.target.value = ''; return; }
    if (!candidate.size || candidate.size > MAX_SIZE) { setError('Wähle eine nicht leere cookies.txt mit höchstens 256 KB.'); event.target.value = ''; return; }
    setFile(candidate);
  }
  async function upload() {
    if (!file || busy) return;
    const site = selectedSite;
    setBusy(true); setError(''); setMessage('');
    try {
      const content = await file.text();
      await api.saveSourceSession(site, content);
      const result = await api.sourceSessions(); setItems(result.items);
      if (selectedSiteRef.current === site) {
        setFile(null); if (fileInput.current) fileInput.current.value = '';
        setMessage('Die Sitzung wurde hinterlegt. Du kannst den Download jetzt erneut versuchen.');
      }
    } catch (cause) { setError(cause instanceof Error ? cause.message : 'Die Sitzung konnte nicht gespeichert werden.'); }
    finally { setBusy(false); }
  }
  async function remove() {
    if (busy) return;
    setBusy(true); setError(''); setMessage('');
    try {
      await api.removeSourceSession(selectedSite);
      const result = await api.sourceSessions(); setItems(result.items);
      setFile(null); if (fileInput.current) fileInput.current.value = '';
      setMessage('Die Sitzung wurde entfernt. Neue Aufträge verwenden sie nicht mehr.');
    } catch (cause) { setError(cause instanceof Error ? cause.message : 'Die Sitzung konnte nicht entfernt werden.'); }
    finally { setBusy(false); }
  }
  return <section className="server-panel source-sessions" aria-labelledby="source-sessions-heading">
    <div className="section-heading"><div><h2 id="source-sessions-heading" ref={heading} tabIndex={-1}>Sitzungen für Quellen</h2><p className="source-intro">Nur nötig, wenn die Quelle eine Anmeldung verlangt.</p></div><Icon name="link" /></div>
    <div className="source-picker" role="group" aria-label="Quelle auswählen">{sourceSites.map(source => {
      const info = items.find(item => item.site === source.site);
      return <button key={source.site} type="button" disabled={busy} aria-pressed={selectedSite === source.site} className={selectedSite === source.site ? 'selected' : ''} onClick={() => chooseSite(source.site)}><span className={`status-dot ${info?.state === 'configured' ? 'good' : info?.state === 'expired' || info?.state === 'invalid' ? 'warning' : ''}`} /><span>{source.label}</span><span className="sr-only">{loading ? 'Wird geladen' : stateLabels[info?.state ?? 'missing']}</span></button>;
    })}</div>
    {selectedSite === 'instagram' && <InstagramAccount api={api} onSessionChange={() => void load()} />}
    <details className="source-manual"><summary>Manuelle Cookies importieren (optional)</summary>
    {loading ? <div className="source-loading" role="status"><span className="spinner" />Sitzungen werden geladen …</div> : <>
      <div className="source-status"><div><h3>{label}</h3><span className={`source-state ${state === 'configured' ? 'good-text' : state === 'expired' || state === 'invalid' ? 'warning-text' : ''}`}>{stateLabels[state]}</span></div>{hasSession && <dl><div><dt>Ablauf</dt><dd>{dateLabel(selected?.expires_at)}</dd></div>{selected?.updated_at && <div><dt>Aktualisiert</dt><dd>{dateLabel(selected.updated_at)}</dd></div>}{selected?.configured && <div><dt>Übernommen</dt><dd>{selected.cookie_count} {selected.cookie_count === 1 ? 'Cookie' : 'Cookies'}</dd></div>}</dl>}</div>
      {state === 'configured' && <p className="source-note">Hinterlegt bedeutet, dass Vela die Datei lesen kann. Die Quelle kann eine Sitzung vorzeitig beenden.</p>}
      {(state === 'expired' || state === 'invalid') && <p className="source-note warning-text">Exportiere eine aktuelle Sitzung der Quelle und ersetze die bisherige Datei.</p>}
      <div className="source-upload"><input ref={fileInput} type="file" accept=".txt,text/plain" className="sr-only" tabIndex={-1} aria-label={`cookies.txt für ${label} auswählen`} onChange={chooseFile} disabled={busy} /><button className="button secondary" type="button" disabled={busy} onClick={() => fileInput.current?.click()}><Icon name="folder" size={17} />{file ? 'Andere Datei wählen' : 'cookies.txt auswählen'}</button><span className="source-file-label">{file ? `Datei ausgewählt · ${formatBytes(file.size)}` : 'Netscape-Format · höchstens 256 KB'}</span></div>
      <div className="source-actions"><button className="button primary" type="button" disabled={!file || busy} onClick={() => void upload()}>{busy ? <span className="spinner" /> : <Icon name="check" size={17} />}Sitzung hinterlegen</button>{hasSession && <button className="button ghost" type="button" disabled={busy} onClick={() => void remove()}><Icon name="close" size={17} />Entfernen</button>}</div>
    </>}
    {error && <div className="notice error" role="alert"><Icon name="alert" size={18} /><span>{error}</span>{!items.length && !loading && <button className="text-button" onClick={() => void load()}>Erneut laden</button>}</div>}
    {message && <div className="notice success" role="status"><Icon name="check" size={18} /><span>{message}</span></div>}
    <details className="source-instructions"><summary>So importierst du Cookies manuell</summary><ol><li>Öffne die gewählte Quelle in deinem Browser und melde dich dort an.</li><li>Exportiere die Cookies dieser Quelle als cookies.txt im Netscape-Format. <a className="text-button" href="https://github.com/yt-dlp/yt-dlp/wiki/FAQ#how-do-i-pass-cookies-to-yt-dlp" target="_blank" rel="noopener noreferrer">Anleitung zum Cookie-Export <Icon name="external" size={13} /></a></li><li>Wähle hier die passende Quelle und die Datei. Klicke anschließend auf „Sitzung hinterlegen“.</li><li>Wiederhole danach den fehlgeschlagenen Download.</li></ol><p>Vela übernimmt ausschließlich erlaubte Domains der gewählten Quelle und verwendet die Sitzung nur für passende direkte Abrufe. Cookie-Werte werden hier nicht angezeigt. Teile die Datei nicht im Chat.</p><p>Hinterlegte Sitzungen stehen Downloads im vertrauenswürdigen Heimnetz zur Verfügung. Vela liest keine persönlichen Browserprofile automatisch aus. Die Sitzung liegt auf deinem Heimserver und kann hier jederzeit entfernt werden.</p></details>
    </details>
  </section>;
}
