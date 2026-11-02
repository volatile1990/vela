import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import type { FormEvent } from 'react';
import { Api, ApiError, active, formatBytes, formatDuration, hostLabel, statusLabels, terminal, validMediaUrl, sourceSiteForUrl } from './api';
import type { DownloadOptions, Job, Overview, Profile, ServerInfo, SourceSite, Storage } from './api';
import { Icon, VelaMark } from './icons';
import SourceSessions from './SourceSessions';

type View = 'downloads' | 'history' | 'server';
type HistoryFilter = 'all' | 'completed' | 'failed' | 'cancelled';
const profileNames: Record<Profile, string> = { original: 'Originalqualität', compatible: 'Kompatibel', audio: 'Nur Audio' };

function ErrorNotice({ children, onDismiss }: { children: React.ReactNode; onDismiss?: () => void }) {
  return <div className="notice error" role="alert"><Icon name="alert" size={19} /><span>{children}</span>{onDismiss && <button className="icon-button" onClick={onDismiss} aria-label="Meldung schließen"><Icon name="close" size={16} /></button>}</div>;
}
function Spinner({ label }: { label?: string }) { return <span className="spinner" aria-label={label} role={label ? 'status' : undefined} />; }

export default function App() {
  const [serverInfo, setServerInfo] = useState<ServerInfo | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const connect = useCallback(async () => {
    setLoading(true); setError('');
    try { setServerInfo(await new Api().serverInfo()); }
    catch (cause) { setError(cause instanceof Error ? cause.message : 'Vela ist nicht erreichbar.'); }
    finally { setLoading(false); }
  }, []);
  useEffect(() => { void connect(); }, [connect]);
  if (loading) return <div className="launch-screen"><VelaMark size={54} /><Spinner label="Vela wird geladen" /><span>Vela wird geladen …</span></div>;
  if (!serverInfo) return <div className="launch-screen"><VelaMark size={54} /><h1>Vela ist nicht erreichbar</h1><p>{error}</p><button className="button primary" onClick={() => void connect()}><Icon name="refresh" />Erneut verbinden</button></div>;
  return <Dashboard version={serverInfo.version} />;
}

function Dashboard({ version }: { version: string }) {
  const api = useMemo(() => new Api(), []);
  const [view, setView] = useState<View>('downloads');
  const [sourceSite, setSourceSite] = useState<SourceSite>('instagram');
  const [focusSourceSessions, setFocusSourceSessions] = useState(false);
  function openSourceSession(site: SourceSite) { setSourceSite(site); setFocusSourceSessions(true); setView('server'); }
  const [jobs, setJobs] = useState<Job[]>([]);
  const [total, setTotal] = useState(0);
  const [overview, setOverview] = useState<Overview | null>(null);
  const [initialLoading, setInitialLoading] = useState(true);
  const [connected, setConnected] = useState(true);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [pendingJobs, setPendingJobs] = useState<Set<string>>(new Set());
  const [loadingMore, setLoadingMore] = useState(false);
  const [historyFilter, setHistoryFilter] = useState<HistoryFilter>('all');
  const [search, setSearch] = useState('');
  const refreshBusy = useRef(false);
  const knownJobs = useRef<Job[]>([]);

  useEffect(() => { document.title = `Vela · ${view === 'downloads' ? 'Downloads' : view === 'history' ? 'Verlauf' : 'Server'}`; }, [view]);
  useEffect(() => { knownJobs.current = jobs; }, [jobs]);

  const handleError = useCallback((cause: unknown) => {
    setError(cause instanceof Error ? cause.message : 'Die Aktion konnte nicht abgeschlossen werden.');
  }, []);
  const refresh = useCallback(async () => {
    if (refreshBusy.current) return;
    refreshBusy.current = true;
    try {
      const [result, info] = await Promise.all([api.jobs(), api.overview()]);
      const olderActive = knownJobs.current.filter(job => active(job.status) && !result.items.some(newJob => newJob.id === job.id));
      const updates = await Promise.all(olderActive.map(job => api.job(job.id)));
      const incoming = [...result.items, ...updates];
      setJobs(previous => [...incoming.map(job => {
        const old = previous.find(item => item.id === job.id);
        return old && old.updated_at > job.updated_at ? old : job;
      }), ...previous.filter(job => !incoming.some(newJob => newJob.id === job.id))]);
      setTotal(result.total); setOverview(info); setConnected(true);
    } catch (cause) { setConnected(false); if (cause instanceof ApiError && cause.status === 401) handleError(cause); }
    finally { setInitialLoading(false); refreshBusy.current = false; }
  }, [api, handleError]);
  useEffect(() => {
    void refresh();
    const interval = window.setInterval(() => void refresh(), 5000);
    const focus = () => void refresh(); window.addEventListener('focus', focus);
    let timer: number | undefined;
    let source: EventSource | undefined;
    if (typeof EventSource !== 'undefined') {
      source = new EventSource('/api/events');
      const changed = () => { if (timer === undefined) timer = window.setTimeout(() => { timer = undefined; void refresh(); }, 350); };
      source.addEventListener('changed', changed); source.addEventListener('snapshot', changed);
    }
    return () => { window.clearInterval(interval); window.removeEventListener('focus', focus); if (timer !== undefined) window.clearTimeout(timer); source?.close(); };
  }, [refresh]);

  const downloads = jobs.filter(job => job.kind !== 'probe');
  const running = downloads.filter(job => active(job.status));
  const history = downloads.filter(job => terminal(job.status));
  const visibleHistory = history.filter(job => historyFilter === 'all' || job.status === historyFilter).filter(job => `${job.title ?? ''} ${job.url}`.toLocaleLowerCase('de').includes(search.toLocaleLowerCase('de')));
  const storage = overview?.storage ?? overview?.worker?.storage;
  const workerReady = Boolean(overview?.worker?.status === 'running' && overview.worker.heartbeat_at && Date.now() - new Date(overview.worker.heartbeat_at).getTime() < 45_000);
  async function action(job: Job, type: 'cancel' | 'retry') {
    if (pendingJobs.has(job.id)) return;
    setPendingJobs(previous => new Set(previous).add(job.id)); setError('');
    try {
      const next = await api[type](job.id); setJobs(previous => previous.map(item => item.id === next.id ? next : item));
      setNotice(type === 'retry' ? 'Der Download steht wieder in der Warteschlange.' : 'Der Abbruch wurde angefordert.');
      void refresh();
    } catch (cause) { handleError(cause); }
    finally { setPendingJobs(previous => { const next = new Set(previous); next.delete(job.id); return next; }); }
  }
  async function loadMore() {
    setLoadingMore(true);
    try { const result = await api.jobs(jobs.length); setJobs(previous => [...previous, ...result.items.filter(item => !previous.some(old => old.id === item.id))]); setTotal(result.total); }
    catch (cause) { handleError(cause); }
    finally { setLoadingMore(false); }
  }
  function onCreated(job: Job) {
    setJobs(previous => [job, ...previous.filter(item => item.id !== job.id)]); setTotal(previous => previous + 1);
    setNotice('Download hinzugefügt. Vela übernimmt ab hier.'); void refresh();
  }
  const navItems: { view: View; icon: 'download' | 'history' | 'server'; label: string; count?: number }[] = [
    { view: 'downloads', icon: 'download', label: 'Downloads', count: running.length },
    { view: 'history', icon: 'history', label: 'Verlauf' }, { view: 'server', icon: 'server', label: 'Server' },
  ];
  return <div className="app-shell">
    <a href="#main-content" className="skip-link">Zum Inhalt springen</a>
    <aside className="sidebar">
      <a className="brand" href="#" onClick={event => { event.preventDefault(); setView('downloads'); }} aria-label="Vela Downloads"><VelaMark /><span>vela<span className="brand-dot">.</span></span></a>
      <div className="sidebar-label">DEINE MEDIEN</div>
      <nav className="main-nav" aria-label="Hauptnavigation">{navItems.map(item => <button key={item.view} onClick={() => setView(item.view)} className={`nav-item ${view === item.view ? 'selected' : ''}`} aria-current={view === item.view ? 'page' : undefined}><Icon name={item.icon} /><span>{item.label}</span>{Boolean(item.count) && <span className="nav-count">{item.count}</span>}</button>)}</nav>
      <div className="sidebar-bottom"><div className="home-indicator"><span className={`status-dot ${connected && workerReady ? 'good' : 'warning'}`} /><span>{connected && workerReady ? 'Zu Hause verbunden' : connected ? 'Worker nicht bereit' : 'Verbindung unterbrochen'}</span></div><span className="sidebar-version">Vela {overview?.version ?? version}</span></div>
    </aside>
    <div className="main-shell">
      <header className="topbar"><div className="mobile-brand"><VelaMark size={31} /><span>vela<span className="brand-dot">.</span></span></div><div className="breadcrumb">Dein Medienarchiv <Icon name="chevron" size={14} /><span>{view === 'downloads' ? 'Downloads' : view === 'history' ? 'Verlauf' : 'Server'}</span></div><span className="connection-label"><span className={`status-dot ${connected ? 'good' : 'warning'}`} />{connected ? 'Verbunden' : 'Offline'}</span></header>
      <main id="main-content" className="main-content" tabIndex={-1}>
        {!connected && <div className="notice warning" role="status"><Icon name="alert" size={19} /><span>Verbindung unterbrochen. Angezeigte Daten können veraltet sein. Vela verbindet sich automatisch neu.</span><button className="text-button" onClick={() => void refresh()}>Erneut versuchen</button></div>}
        {error && <ErrorNotice onDismiss={() => setError('')}>{error}</ErrorNotice>}
        <div className="sr-only" role="status" aria-live="polite">{notice}</div>
        {view === 'downloads' && <>
          <div className="page-heading"><div><div className="eyebrow">DOWNLOADS</div><h1>Was möchtest du speichern?</h1><p>Ein Link zu deinem nächsten Video oder Audio genügt.</p></div><span className="quiet-tag"><Icon name="folder" size={15} /> Auf deinem Server</span></div>
          <div className="download-layout"><div className="download-primary"><DownloadComposer api={api} onCreated={onCreated} onError={handleError} onSourceSession={openSourceSession} />
            <section className="queue-section" aria-labelledby="queue-heading"><div className="section-heading"><h2 id="queue-heading">Warteschlange <span className="count-badge">{running.length}</span></h2><span className="subtle">{running.length ? 'Vela arbeitet für dich' : 'Bereit für deinen nächsten Link'}</span></div>
              {initialLoading ? <ListSkeleton /> : running.length ? <div className="job-list">{running.map(job => <JobRow key={job.id} job={job} pending={pendingJobs.has(job.id)} onAction={action} onSourceSession={openSourceSession} />)}</div> : <div className="empty-state"><span className="empty-icon"><Icon name="queue" size={25} /></span><div><h3>Alles ist erledigt.</h3><p>Neue Downloads erscheinen hier. Du kannst Vela danach geöffnet lassen oder später wiederkommen.</p></div></div>}
            </section>
            {history.length > 0 && <section className="recent-section" aria-labelledby="recent-heading"><div className="section-heading"><h2 id="recent-heading">Letzte Aufträge</h2><button className="text-button" onClick={() => setView('history')}>Zum Verlauf <Icon name="arrow" size={15} /></button></div><div className="job-list">{history.slice(0, 3).map(job => <JobRow key={job.id} job={job} pending={pendingJobs.has(job.id)} onAction={action} onSourceSession={openSourceSession} compact />)}</div></section>}
          </div><aside className="context-column"><StorageCard storage={storage} /><div className="server-note"><div className="section-heading"><span className="mini-title">DEIN SERVER</span><Icon name="server" size={17} /></div><p><span className={`status-dot ${workerReady ? 'good' : 'warning'}`} />{workerReady ? 'Bereit für Downloads' : 'Wartet auf Verbindung'}</p><span>Downloads laufen auf deinem Server weiter, auch wenn du den Browser schließt.</span><button className="text-button" onClick={() => setView('server')}>Serverstatus ansehen <Icon name="arrow" size={15} /></button></div></aside></div>
        </>}
        {view === 'history' && <>
          <div className="page-heading"><div><div className="eyebrow">VERLAUF</div><h1>Deine Downloads.</h1><p>Gespeicherte Dateien und vergangene Aufträge an einem Ort.</p></div></div>
          <section className="history-panel"><div className="history-toolbar"><div className="filter-group" role="group" aria-label="Verlauf filtern">{(['all', 'completed', 'failed', 'cancelled'] as HistoryFilter[]).map(filter => <button key={filter} className={historyFilter === filter ? 'selected' : ''} aria-pressed={historyFilter === filter} onClick={() => setHistoryFilter(filter)}>{({ all: 'Alle', completed: 'Abgeschlossen', failed: 'Fehler', cancelled: 'Abgebrochen' })[filter]}</button>)}</div><input type="search" className="history-search" aria-label="Verlauf durchsuchen" placeholder="Titel oder Quelle suchen" value={search} onChange={event => setSearch(event.target.value)} /></div>
          {initialLoading ? <ListSkeleton /> : visibleHistory.length ? <div className="job-list">{visibleHistory.map(job => <JobRow key={job.id} job={job} pending={pendingJobs.has(job.id)} onAction={action} onSourceSession={openSourceSession} />)}</div> : <div className="empty-state spacious"><span className="empty-icon"><Icon name="history" size={27} /></span><div><h3>{search || historyFilter !== 'all' ? 'Keine passenden Downloads.' : 'Dein Verlauf beginnt hier.'}</h3><p>{search || historyFilter !== 'all' ? 'Versuche einen anderen Suchbegriff oder Filter.' : 'Sobald dein erster Download fertig ist, findest du ihn hier.'}</p></div></div>}
          {jobs.length < total && <button className="button secondary load-more" onClick={() => void loadMore()} disabled={loadingMore}>{loadingMore && <Spinner />}Ältere Aufträge laden</button>}
          </section>
        </>}
        {view === 'server' && <ServerView api={api} sourceSite={sourceSite} focusSourceSessions={focusSourceSessions} overview={overview} connected={connected} workerReady={workerReady} loading={initialLoading} onRefresh={() => void refresh()} />}
        <footer className="content-footer"><span>Deine Medien bleiben bei dir.</span><span>Vela {overview?.version ?? version}</span></footer>
      </main>
    </div>
  </div>;
}

function DownloadComposer({ api, onCreated, onError, onSourceSession }: { api: Api; onCreated: (job: Job) => void; onError: (error: unknown) => void; onSourceSession: (site: SourceSite) => void }) {
  const [url, setUrl] = useState(() => new URLSearchParams(window.location.search).get('url') ?? '');
  const [profile, setProfile] = useState<Profile>('original');
  const [maxHeight, setMaxHeight] = useState(1080);
  const [playlist, setPlaylist] = useState(false);
  const [playlistItems, setPlaylistItems] = useState('');
  const [preview, setPreview] = useState<Job | null>(null);
  const [previewLoading, setPreviewLoading] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState('');
  const [success, setSuccess] = useState('');
  const [clipboardMessage, setClipboardMessage] = useState('');
  const urlInput = useRef<HTMLInputElement>(null);
  const previewGeneration = useRef(0);
  const idempotencyKey = useRef('');

  useEffect(() => {
    if (!preview || terminal(preview.status)) return;
    let disposed = false;
    const timer = window.setInterval(async () => {
      try { const job = await api.job(preview.id); if (!disposed) setPreview(job); }
      catch (cause) { if (!disposed) { setError(cause instanceof Error ? cause.message : 'Vorschau konnte nicht geladen werden.'); setPreview(null); } }
    }, 1500);
    return () => { disposed = true; window.clearInterval(timer); };
  }, [api, preview]);

  function validate(): DownloadOptions | null {
    if (!validMediaUrl(url.trim())) { setError('Gib einen vollständigen Link mit https:// oder http:// ein.'); urlInput.current?.focus(); return null; }
    if (playlist && playlistItems.trim() && !/^\d+(?:-\d+)?(?:\s*,\s*\d+(?:-\d+)?)*$/.test(playlistItems.trim())) { setError('Gib Einträge als Zahlen oder Bereiche ein, zum Beispiel 1–5 mit einem Bindestrich: 1-5, 8.'); return null; }
    setError(''); return { url: url.trim(), profile, max_height: profile === 'audio' ? null : maxHeight, playlist, playlist_items: playlist && playlistItems.trim() ? playlistItems.replace(/\s/g, '') : null };
  }
  async function submit(event: FormEvent) {
    event.preventDefault(); if (submitting) return;
    const options = validate(); if (!options) return;
    if (!idempotencyKey.current) idempotencyKey.current = crypto.randomUUID();
    setSubmitting(true); setSuccess('');
    try {
      const job = await api.create(options, idempotencyKey.current); onCreated(job); setSuccess('Hinzugefügt. Dein Download steht in der Warteschlange.');
      setUrl(''); setPreview(null); previewGeneration.current++; idempotencyKey.current = '';
    } catch (cause) { if (cause instanceof ApiError && cause.status === 401) onError(cause); else setError(cause instanceof Error ? cause.message : 'Download konnte nicht hinzugefügt werden.'); }
    finally { setSubmitting(false); }
  }
  async function loadPreview() {
    const options = validate(); if (!options || previewLoading) return;
    setPreviewLoading(true); setPreview(null); setSuccess('');
    const generation = ++previewGeneration.current;
    try { const job = await api.preview(options); if (generation === previewGeneration.current) setPreview(job); }
    catch (cause) { if (cause instanceof ApiError && cause.status === 401) onError(cause); else if (generation === previewGeneration.current) setError(cause instanceof Error ? cause.message : 'Vorschau konnte nicht geladen werden.'); }
    finally { setPreviewLoading(false); }
  }
  async function paste() {
    try { const value = await navigator.clipboard.readText(); setUrl(value.trim()); setPreview(null); previewGeneration.current++; idempotencyKey.current = ''; setSuccess(''); setError(''); setClipboardMessage('Link aus der Zwischenablage eingefügt.'); urlInput.current?.focus(); }
    catch { setClipboardMessage('Füge deinen Link mit dem Einfügen-Befehl deines Geräts ein.'); urlInput.current?.focus(); }
  }
  const previewPending = previewLoading || Boolean(preview && !terminal(preview.status));
  return <section className="composer" aria-labelledby="composer-heading">
    <div className="composer-heading"><div className="composer-icon"><Icon name="link" size={21} /></div><div><h2 id="composer-heading">Neuer Download</h2><span>Video, Audio oder eine Playlist</span></div></div>
    <form onSubmit={submit} noValidate aria-busy={submitting}><fieldset className="composer-controls" disabled={submitting}>
      <label htmlFor="media-url" className="field-label">Medienlink</label>
      <div className={`url-input-wrap ${error && !validMediaUrl(url) ? 'invalid' : ''}`}><Icon name="link" size={19} /><input ref={urlInput} id="media-url" type="url" inputMode="url" autoCapitalize="none" spellCheck={false} autoComplete="off" placeholder="https:// …" value={url} aria-describedby={clipboardMessage ? 'clipboard-message' : undefined} onChange={event => { setUrl(event.target.value); setError(''); setSuccess(''); setPreview(null); previewGeneration.current++; idempotencyKey.current = ''; }} required /><button type="button" className="paste-button" onClick={() => void paste()} title="Aus Zwischenablage einfügen"><Icon name="clipboard" size={17} /><span>Einfügen</span></button></div>
      {clipboardMessage && <p id="clipboard-message" className="field-hint" role="status">{clipboardMessage}</p>}
      <fieldset className="profile-fieldset"><legend className="field-label">Ausgabeprofil</legend><div className="profile-options">
        {([{ key: 'original', icon: 'video', detail: 'Beste verfügbare Qualität' }, { key: 'compatible', icon: 'film', detail: 'Für viele Geräte geeignet' }, { key: 'audio', icon: 'audio', detail: 'Ohne Videospur' }] as const).map(item => <label className={`profile-option ${profile === item.key ? 'selected' : ''}`} key={item.key}><input type="radio" name="profile" value={item.key} checked={profile === item.key} onChange={() => { setProfile(item.key); idempotencyKey.current = ''; }} /><Icon name={item.icon} size={20} /><span><strong>{item.key === 'original' ? 'Original' : profileNames[item.key]}</strong><small>{item.detail}</small></span><span className="radio-indicator">{profile === item.key && <span />}</span></label>)}
      </div></fieldset>
      <div className="options-line"><div className="quality-field"><label htmlFor="quality">{profile === 'audio' ? 'Audioqualität' : 'Max. Auflösung'}</label>{profile === 'audio' ? <span className="audio-quality">Beste verfügbare <Icon name="audio" size={15} /></span> : <select id="quality" value={maxHeight} onChange={event => { setMaxHeight(Number(event.target.value)); idempotencyKey.current = ''; }}><option value={2160}>4K · 2160p</option><option value={1080}>Full HD · 1080p</option><option value={720}>HD · 720p</option><option value={480}>SD · 480p</option></select>}</div><label className="checkbox-label"><input type="checkbox" checked={playlist} onChange={event => { setPlaylist(event.target.checked); setPreview(null); previewGeneration.current++; idempotencyKey.current = ''; }} /><span>Playlist herunterladen</span></label></div>
      {playlist && <div className="playlist-options"><label htmlFor="playlist-items">Playlist-Einträge <span className="subtle">optional</span></label><input id="playlist-items" placeholder="Alle Einträge oder z. B. 1-5, 8" value={playlistItems} onChange={event => { setPlaylistItems(event.target.value); idempotencyKey.current = ''; }} /><p className="field-hint">Große Playlists werden durch das Serverlimit begrenzt.</p></div>}
      {profile === 'compatible' && <p className="profile-hint">Lädt H.264 und AAC als MP4. Ist keine passende Quelle verfügbar, meldet Vela einen Fehler.</p>}
      {previewPending && <div className="preview-loading" role="status"><Spinner /><div><strong>Vorschau wird geladen</strong><span>Vela prüft den Link auf deinem Server.</span></div></div>}
      {preview?.status === 'completed' && <PreviewCard job={preview} />}
      {preview?.status === 'failed' && <><ErrorNotice>{preview.error ?? 'Diese Vorschau konnte nicht geladen werden. Prüfe den Link oder versuche es erneut.'}</ErrorNotice>{sessionSite(preview) && <button className="text-button session-shortcut" type="button" onClick={() => onSourceSession(sessionSite(preview)!)}>{sessionSite(preview) === 'instagram' ? 'Instagram bestätigen' : 'Sitzung hinterlegen'} <Icon name="arrow" size={15} /></button>}</>}
      {error && <ErrorNotice>{error}</ErrorNotice>}
      {success && <div className="notice success" role="status"><Icon name="check" size={19} /><span>{success}</span></div>}
      <div className="composer-footer"><button type="button" className="button ghost" onClick={() => void loadPreview()} disabled={!url.trim() || previewPending || submitting}>{previewPending ? <Spinner /> : <Icon name="eye" size={18} />}Vorschau</button><button className="button primary" type="submit" disabled={!url.trim() || submitting}>{submitting ? <Spinner /> : <Icon name="download" size={18} />}{submitting ? 'Wird hinzugefügt …' : 'Download starten'}</button></div>
    </fieldset></form>
  </section>;
}

function PreviewCard({ job }: { job: Job }) {
  const entries = Array.isArray(job.metadata?.entries) ? job.metadata.entries : [];
  return <div className="preview-card"><div className="preview-art"><Icon name={entries.length ? 'queue' : 'video'} size={28} /></div><div className="preview-info"><span className="mini-title">VORSCHAU</span><h3>{job.metadata?.title || job.title || 'Medien erkannt'}</h3><p>{hostLabel(job.url)}{typeof job.metadata?.duration === 'number' && <> <span>·</span> {formatDuration(job.metadata.duration)}</>}{entries.length > 0 && <> <span>·</span> {entries.length} Einträge erkannt</>}</p>{entries.length > 0 && <details className="preview-entries"><summary>Einträge ansehen</summary><ol>{entries.slice(0, 10).map((entry, index) => <li key={`${entry.id ?? index}-${index}`}>{entry.title || `Eintrag ${index + 1}`}</li>)}</ol>{entries.length > 10 && <p>und {entries.length - 10} weitere</p>}</details>}</div><span className="preview-check"><Icon name="check" size={18} /></span></div>;
}

function JobRow({ job, pending, onAction, onSourceSession, compact = false }: { job: Job; pending: boolean; onAction: (job: Job, action: 'cancel' | 'retry') => Promise<void>; onSourceSession: (site: SourceSite) => void; compact?: boolean }) {
  const isActive = active(job.status);
  const progress = job.progress == null ? null : Math.min(100, Math.max(0, job.progress));
  const [showDetails, setShowDetails] = useState(false);
  const files = job.artifacts ?? [];
  const statusText = job.cancel_requested ? 'Wird abgebrochen' : statusLabels[job.status] ?? 'Wird bearbeitet';
  const date = new Date(job.created_at);
  return <article className={`job-row ${isActive ? 'is-active' : ''} ${compact ? 'compact' : ''}`} aria-label={job.title || hostLabel(job.url)}>
    <div className={`job-art ${job.profile === 'audio' ? 'audio' : ''}`}><Icon name={job.profile === 'audio' ? 'audio' : job.playlist ? 'queue' : 'video'} size={23} /></div>
    <div className="job-body"><div className="job-title-line"><h3 title={job.title ?? undefined}>{job.title || hostLabel(job.url)}</h3><span className={`status-label ${job.status}`}>{job.status === 'completed' && <Icon name="check" size={13} />}{job.status === 'failed' && <Icon name="alert" size={13} />}{statusText}</span></div>
      <div className="job-meta"><span>{hostLabel(job.url)}</span><span className="meta-divider">·</span><span>{profileNames[job.profile]}{job.max_height && job.profile !== 'audio' ? ` · ${job.max_height}p` : ''}</span>{!isActive && <><span className="meta-divider">·</span><time dateTime={job.created_at} title={date.toLocaleString('de-DE')}>{date.toLocaleDateString('de-DE', { day: '2-digit', month: 'short' })}</time></>}</div>
      {isActive && job.status !== 'queued' && <div className="job-progress-area"><div className={`progress-track ${progress == null ? 'indeterminate' : ''}`} role="progressbar" aria-label={`Fortschritt für ${job.title || 'Download'}`} aria-valuemin={0} aria-valuemax={100} aria-valuenow={progress ?? undefined} aria-valuetext={progress == null ? statusText : `${Math.round(progress)} Prozent, ${statusText}`}><div style={{ width: progress == null ? '32%' : `${progress}%` }} /></div><div className="progress-details"><span>{progress != null ? `${Math.round(progress)} %` : statusText}{job.downloaded_bytes != null && <> <span className="meta-divider">·</span> {formatBytes(job.downloaded_bytes)}{job.total_bytes ? ` / ${formatBytes(job.total_bytes)}` : ''}</>}</span><span>{job.speed ? `${formatBytes(job.speed)}/s` : ''}{job.eta != null && job.eta > 0 ? ` · noch ${formatDuration(job.eta)}` : ''}</span></div></div>}
      {job.status === 'waiting_storage' && <p className="job-help">Der Zielspeicher ist gerade nicht verfügbar. Vela versucht es erneut.</p>}
      {job.error && <div className="job-error"><Icon name="alert" size={14} /><span>{job.error}</span></div>}
      {sessionSite(job) && <button className="text-button session-shortcut" onClick={() => onSourceSession(sessionSite(job)!)}>{sessionSite(job) === 'instagram' ? 'Instagram bestätigen' : 'Sitzung hinterlegen'} <Icon name="arrow" size={14} /></button>}
      {files.length > 1 && <button className="text-button files-toggle" aria-expanded={showDetails} onClick={() => setShowDetails(!showDetails)}>{files.length} Dateien · {formatBytes(files.reduce((sum, file) => sum + file.size, 0))}<Icon name="chevron" size={13} style={{ transform: showDetails ? 'rotate(90deg)' : undefined }} /></button>}
      {files.length === 1 && !isActive && <div className="file-meta">{formatBytes(files[0].size)}<span className="meta-divider">·</span>{files[0].name.split('.').pop()?.toUpperCase()}</div>}
      {showDetails && <ul className="artifact-list">{files.map(file => <li key={file.id}><a href={`/api/artifacts/${encodeURIComponent(file.id)}/download`} download><Icon name="download" size={15} /><span>{file.name}</span><small>{formatBytes(file.size)}</small></a></li>)}</ul>}
    </div>
    <div className="job-actions">{isActive ? <button className="icon-button" onClick={() => void onAction(job, 'cancel')} aria-label={`Download abbrechen: ${job.title || hostLabel(job.url)}`} title="Download abbrechen" disabled={pending || job.cancel_requested}>{pending ? <Spinner /> : <Icon name="close" size={18} />}</button> : <>
      {files.length > 0 && <a className="icon-button download-action" href={`/api/artifacts/${encodeURIComponent(files[0].id)}/download`} download aria-label={`Datei herunterladen: ${files[0].name}`} title="Datei herunterladen"><Icon name="download" size={19} /></a>}
      {(job.status === 'failed' || job.status === 'cancelled') && <button className="icon-button" onClick={() => void onAction(job, 'retry')} aria-label={`Download wiederholen: ${job.title || hostLabel(job.url)}`} title="Erneut versuchen" disabled={pending}>{pending ? <Spinner /> : <Icon name="retry" size={18} />}</button>}
      <a className="icon-button source-action" href={validMediaUrl(job.url) ? job.url : undefined} target="_blank" rel="noopener noreferrer" aria-label={`Quelle öffnen: ${job.title || hostLabel(job.url)}`} title="Quelle öffnen"><Icon name="external" size={16} /></a>
    </>}</div>
  </article>;
}

function StorageCard({ storage }: { storage: Storage | undefined }) {
  const usedRatio = storage?.total_bytes && storage.free_bytes != null ? Math.min(100, Math.max(0, (storage.total_bytes - storage.free_bytes) / storage.total_bytes * 100)) : null;
  return <section className="storage-card" aria-labelledby="storage-heading"><div className="section-heading"><span className="mini-title" id="storage-heading">SPEICHER</span><span className={`storage-status ${storage?.available ? 'good-text' : ''}`}><span className={`status-dot ${storage?.available ? 'good' : 'warning'}`} />{storage?.available ? 'Verbunden' : storage ? 'Nicht verfügbar' : 'Wird geprüft'}</span></div><div className="storage-icon"><Icon name="folder" size={24} /></div><h3>{storage?.label || 'Medienarchiv'}</h3><div className="storage-free">{formatBytes(storage?.free_bytes)}<span> frei</span></div>{usedRatio !== null && <div className="storage-track" role="meter" aria-label="Speicherbelegung" aria-valuemin={0} aria-valuemax={100} aria-valuenow={Math.round(usedRatio)}><span style={{ width: `${usedRatio}%` }} /></div>}<p>{storage?.total_bytes ? `von ${formatBytes(storage.total_bytes)} Gesamtspeicher` : 'Speicherinformationen werden vom Server geladen.'}</p>{storage?.error && <p className="storage-error">Der Speicher ist vorübergehend nicht erreichbar.</p>}</section>;
}

function ServerView({ api, sourceSite, focusSourceSessions, overview, connected, workerReady, loading, onRefresh }: { api: Api; sourceSite: SourceSite; focusSourceSessions: boolean; overview: Overview | null; connected: boolean; workerReady: boolean; loading: boolean; onRefresh: () => void }) {
  const storage = overview?.storage ?? overview?.worker?.storage;
  const engine = overview?.worker?.engine;
  return <><div className="page-heading"><div><div className="eyebrow">SERVER</div><h1>Alles im Blick.</h1><p>Verbindung, Speicher und der aktuelle Betrieb von Vela.</p></div><button className="button secondary" onClick={onRefresh}><Icon name="refresh" size={17} />Aktualisieren</button></div>
  {loading ? <ListSkeleton /> : <div className="server-grid"><section className="server-panel"><div className="section-heading"><h2>Verbindung</h2><Icon name="server" /></div><dl className="status-list"><div><dt>Vela</dt><dd><span className={`status-dot ${connected ? 'good' : 'warning'}`} />{connected ? 'Erreichbar' : 'Nicht erreichbar'}</dd></div><div><dt>Download-Dienst</dt><dd><span className={`status-dot ${workerReady ? 'good' : 'warning'}`} />{workerReady ? 'Bereit' : 'Keine aktuelle Verbindung'}</dd></div><div><dt>Letzte Rückmeldung</dt><dd>{overview?.worker?.heartbeat_at ? new Date(overview.worker.heartbeat_at).toLocaleTimeString('de-DE', { hour: '2-digit', minute: '2-digit', second: '2-digit' }) : 'Noch keine'}</dd></div><div><dt>Version</dt><dd>{overview?.version ?? '–'}</dd></div></dl>{engine && <details className="engine-details"><summary>Versionsdetails</summary>{typeof engine === 'string' ? <p>{engine}</p> : <dl>{Object.entries(engine).filter(([, value]) => ['string', 'number'].includes(typeof value)).map(([key, value]) => <div key={key}><dt>{key}</dt><dd>{String(value)}</dd></div>)}</dl>}</details>}</section><StorageCard storage={storage} /><section className="server-panel activity-summary"><h2>Aufträge</h2><div className="stats-grid">{([{ label: 'In der Warteschlange', value: overview?.counts?.queued }, { label: 'In Bearbeitung', value: overview?.counts?.active }, { label: 'Abgeschlossen', value: overview?.counts?.completed }, { label: 'Fehlgeschlagen', value: overview?.counts?.failed }]).map(item => <div key={item.label}><strong>{item.value ?? '–'}</strong><span>{item.label}</span></div>)}</div></section></div>}
  <SourceSessions api={api} initialSite={sourceSite} focusOnOpen={focusSourceSessions} />
  </>;
}
function sessionSite(job: Job): SourceSite | null { return job.status === 'waiting_auth' || ['audience_restricted', 'login_required', 'session_invalid'].includes(job.error_code ?? '') ? sourceSiteForUrl(job.url) : null; }
function ListSkeleton() { return <div className="list-skeleton" aria-busy="true" aria-label="Aufträge werden geladen"><span /><span /><span /></div>; }
