import { useCallback, useEffect, useRef, useState } from 'react';
import type { FormEvent, MouseEvent } from 'react';
import { Api } from './api';
import type { InstagramAccountStatus, InstagramAction } from './api';
import { Icon } from './icons';

const stateLabels: Record<InstagramAccountStatus['state'], string> = {
  unconfigured: 'Nicht verbunden', idle: 'Zugang hinterlegt', logging_in: 'Anmeldung läuft',
  ready: 'Verbunden', needs_attention: 'Bestätigung erforderlich', error: 'Anmeldung fehlgeschlagen',
};
const keys = [{ key: 'Enter', label: 'Enter' }, { key: 'Tab', label: 'Tab' }, { key: 'Backspace', label: 'Löschen' },
  { key: 'ArrowLeft', label: '←' }, { key: 'ArrowUp', label: '↑' }, { key: 'ArrowDown', label: '↓' }, { key: 'ArrowRight', label: '→' }];

export default function InstagramAccount({ api, onSessionChange }: { api: Api; onSessionChange?: () => void }) {
  const [account, setAccount] = useState<InstagramAccountStatus | null>(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [statusError, setStatusError] = useState('');
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [liveOpen, setLiveOpen] = useState(false);
  const [screenUrl, setScreenUrl] = useState<string | null>(null);
  const [screenReady, setScreenReady] = useState(false);
  const [screenError, setScreenError] = useState('');
  const [actionBusy, setActionBusy] = useState(false);
  const [text, setText] = useState('');
  const [zoomed, setZoomed] = useState(false);
  const pending = useRef(false);
  const statusFetching = useRef(false);
  const operation = useRef(0);
  const image = useRef<HTMLImageElement>(null);
  const mounted = useRef(true);
  const previousSession = useRef<string | null>(null);
  const loadStatus = useCallback(async (signal?: AbortSignal) => {
    if (pending.current || statusFetching.current) return;
    statusFetching.current = true;
    const generation = operation.current;
    try {
      const next = await api.instagramAccount(signal);
      if (mounted.current && !signal?.aborted && generation === operation.current) { setAccount(next); setStatusError(''); }
    } catch {
      if (mounted.current && !signal?.aborted && generation === operation.current) setStatusError('Der Instagram-Status konnte nicht geladen werden. Bitte versuche es erneut.');
    } finally { statusFetching.current = false; if (mounted.current && !signal?.aborted) setLoading(false); }
  }, [api]);

  useEffect(() => {
    mounted.current = true;
    const controller = new AbortController();
    void loadStatus(controller.signal);
    const reload = () => { if (!document.hidden) void loadStatus(controller.signal); };
    const timer = window.setInterval(reload, 2000);
    document.addEventListener('visibilitychange', reload);
    return () => { mounted.current = false; controller.abort(); window.clearInterval(timer); document.removeEventListener('visibilitychange', reload); };
  }, [loadStatus]);
  useEffect(() => { if (!account?.browser_available) { setLiveOpen(false); setText(''); } }, [account?.browser_available]);
  useEffect(() => {
    if (!account) return;
    const current = `${account.configured}:${account.session_generation}`;
    if (previousSession.current !== null && previousSession.current !== current) onSessionChange?.();
    previousSession.current = current;
  }, [account, onSessionChange]);

  useEffect(() => {
    setScreenUrl(null); setScreenReady(false); setScreenError('');
    if (!liveOpen || !account?.browser_available) return;
    let disposed = false;
    let fetching = false;
    let objectUrl: string | null = null;
    const controller = new AbortController();
    const update = async () => {
      if (disposed || fetching || document.hidden) return;
      fetching = true;
      try {
        const blob = await api.instagramScreen(controller.signal);
        if (disposed || document.hidden) return;
        const next = URL.createObjectURL(blob);
        const previous = objectUrl; objectUrl = next;
        setScreenUrl(next); setScreenError('');
        if (previous) URL.revokeObjectURL(previous);
      } catch {
        if (!disposed) setScreenError('Das Livebild ist gerade nicht verfügbar. Es wird erneut versucht.');
      } finally { fetching = false; }
    };
    void update();
    const timer = window.setInterval(() => void update(), 2000);
    const visibility = () => { if (!document.hidden) void update(); };
    document.addEventListener('visibilitychange', visibility);
    return () => { disposed = true; controller.abort(); window.clearInterval(timer); document.removeEventListener('visibilitychange', visibility); if (objectUrl) URL.revokeObjectURL(objectUrl); };
  }, [api, liveOpen, account?.browser_available, account?.interaction_id]);

  async function change(kind: 'save' | 'login' | 'remove', event?: FormEvent) {
    event?.preventDefault();
    if (pending.current || (kind === 'save' && (!username.trim() || !password))) return;
    pending.current = true; operation.current++;
    setBusy(true); setError('');
    const submittedUsername = username.trim(); const submittedPassword = password;
    setPassword(''); if (kind === 'save') setUsername('');
    try {
      const next = kind === 'save' ? await api.saveInstagramAccount(submittedUsername, submittedPassword) : kind === 'login' ? await api.loginInstagramAccount() : await api.removeInstagramAccount();
      if (mounted.current) { setAccount(next); if (kind === 'remove') { setUsername(''); setPassword(''); setLiveOpen(false); setText(''); } }
    } catch {
      if (mounted.current) setError(kind === 'remove' ? 'Der Zugang konnte nicht entfernt werden. Bitte versuche es erneut.' : 'Die Anmeldung konnte nicht gestartet werden. Prüfe den Zugang oder versuche es erneut.');
    } finally { pending.current = false; if (mounted.current) setBusy(false); }
  }
  async function send(action: Omit<InstagramAction, 'interaction_id'>) {
    if (pending.current || !account?.interaction_id || !account.browser_available) return;
    const id = account.interaction_id;
    pending.current = true; operation.current++; setActionBusy(true); setError('');
    if (action.kind === 'text') setText('');
    try { const next = await api.instagramAction({ ...action, interaction_id: id }); if (mounted.current) setAccount(next); }
    catch { if (mounted.current) setError('Die Eingabe konnte nicht übertragen werden. Prüfe das aktuelle Livebild und versuche es erneut.'); }
    finally { pending.current = false; if (mounted.current) setActionBusy(false); }
  }
  function clickImage(event: MouseEvent<HTMLButtonElement>) {
    if (event.detail === 0) { void send({ kind: 'key', key: 'Enter' }); return; }
    const target = image.current; const rect = target?.getBoundingClientRect();
    if (!target?.naturalWidth || !target.naturalHeight || !rect?.width || !rect.height) return;
    const pixelX = (event.clientX - rect.left) * target.naturalWidth / rect.width;
    const pixelY = (event.clientY - rect.top) * target.naturalHeight / rect.height;
    void send({ kind: 'click', x: Math.min(1, Math.max(0, pixelX / target.naturalWidth)), y: Math.min(1, Math.max(0, pixelY / target.naturalHeight)) });
  }
  const status = account?.state && stateLabels[account.state] ? stateLabels[account.state] : 'Wird geprüft';
  const credentialsForm = <form className="ig-credentials" onSubmit={event => void change('save', event)} autoComplete="off"><fieldset disabled={busy || actionBusy}><div className="ig-fields"><div><label htmlFor="ig-username">Instagram-Benutzername</label><input id="ig-username" name="instagram-username" autoComplete="off" autoCapitalize="none" spellCheck={false} value={username} onChange={event => setUsername(event.target.value)} required /></div><div><label htmlFor="ig-password">Instagram-Passwort</label><input id="ig-password" name="instagram-password" type="password" autoComplete="off" value={password} onChange={event => setPassword(event.target.value)} required /></div></div><button className="button primary" disabled={!username.trim() || !password || busy || actionBusy}>{busy ? <span className="spinner" /> : <Icon name="link" size={17} />}Speichern und anmelden</button></fieldset></form>;
  return <div className="ig-account" aria-labelledby="ig-account-heading"><div className="section-heading"><div><h3 id="ig-account-heading">Instagram verbinden</h3><p className="source-intro">Vela meldet sich direkt bei Instagram an. Nur wenn Instagram eine Bestätigung verlangt, greifst du hier ein.</p></div><span className={`ig-account-state ${account?.state === 'ready' ? 'good-text' : account?.state === 'needs_attention' || account?.state === 'error' ? 'warning-text' : ''}`} role="status">{loading ? 'Wird geladen …' : status}</span></div>
    {account?.message && <p className="ig-status-message">{account.message}</p>}
    {!loading && (account?.configured ? <><div className="ig-account-actions"><button className="button secondary" onClick={() => void change('login')} disabled={busy || actionBusy || account.state === 'logging_in'}>{busy || account.state === 'logging_in' ? <span className="spinner" /> : <Icon name="retry" size={17} />}{account.state === 'error' || account.state === 'needs_attention' ? 'Erneut versuchen' : 'Anmelden'}</button><button className="button ghost" onClick={() => void change('remove')} disabled={busy || actionBusy}>Zugang entfernen</button></div><p className="source-note">„Zugang entfernen“ löscht auch das Browserprofil und die Instagram-Sitzung.</p><details className="ig-replace"><summary>Zugang ersetzen</summary>{credentialsForm}</details></> : credentialsForm)}
    {account?.browser_available && <div className="ig-confirmation"><button className={`button ${account.state === 'needs_attention' ? 'primary' : 'secondary'}`} onClick={() => { setLiveOpen(!liveOpen); setZoomed(false); setText(''); }} aria-expanded={liveOpen} aria-controls="ig-live-browser"><Icon name="eye" size={17} />{liveOpen ? 'Bestätigung schließen' : 'Bestätigung öffnen'}</button>{!liveOpen && <p className="source-note">Das Livebild wird erst beim Öffnen geladen.</p>}</div>}
    {liveOpen && account?.browser_available && <div id="ig-live-browser" className="ig-live-browser"><div className="ig-live-heading"><h4>Bestätigung bei Instagram</h4><button className="text-button" onClick={() => setZoomed(!zoomed)}>{zoomed ? 'An Breite anpassen' : 'Bild vergrößern'}</button></div><p className="source-note">Klicke im Livebild auf das gewünschte Feld. Gib danach Text oder einen Bestätigungscode ein. Mit „Tab“ kannst du Felder auch ohne Maus auswählen.</p><div className="ig-screen-scroll">{screenUrl ? <button className={`ig-screen-target ${zoomed ? 'zoomed' : ''}`} type="button" onClick={clickImage} disabled={actionBusy || busy || !screenReady || !account.interaction_id} aria-label="Im Instagram-Browser klicken; Eingabetaste sendet Enter"><img ref={image} src={screenUrl} alt="Aktuelles Livebild des Instagram-Browsers" onLoad={() => setScreenReady(true)} draggable={false} /></button> : <div className="source-loading" role="status"><span className="spinner" />Livebild wird geladen …</div>}</div>{screenError && <p className="source-note warning-text" role="status">{screenError}</p>}<form className="ig-text-input" onSubmit={event => { event.preventDefault(); if (text) void send({ kind: 'text', text }); }}><label htmlFor="ig-browser-text">Text oder Bestätigungscode</label><div><input id="ig-browser-text" value={text} onChange={event => setText(event.target.value)} autoComplete="off" spellCheck={false} maxLength={4096} disabled={actionBusy || busy || !account.interaction_id} /><button className="button primary" disabled={!text || actionBusy || busy || !account.interaction_id}>{actionBusy ? <span className="spinner" /> : <Icon name="arrow" size={17} />}Text eingeben</button></div></form><div className="ig-browser-keys" role="group" aria-label="Tasten im Instagram-Browser">{keys.map(item => <button className="button secondary" key={item.key} type="button" aria-label={`Taste ${item.key} senden`} onClick={() => void send({ kind: 'key', key: item.key })} disabled={actionBusy || busy || !account.interaction_id}>{item.label}</button>)}</div><div className="ig-scroll-actions"><button className="button ghost" onClick={() => void send({ kind: 'scroll', delta: -500 })} disabled={actionBusy || busy || !account.interaction_id}>Nach oben scrollen</button><button className="button ghost" onClick={() => void send({ kind: 'scroll', delta: 500 })} disabled={actionBusy || busy || !account.interaction_id}>Nach unten scrollen</button></div><p className="ig-privacy-note">Livebilder werden hier nicht gespeichert. Hinterlegte Zugänge stehen Downloads im vertrauenswürdigen Heimnetz zur Verfügung.</p></div>}
    {(error || statusError) && <div className="notice error" role="alert"><Icon name="alert" size={18} /><span>{error || statusError}</span><button className="text-button" onClick={() => { setError(''); void loadStatus(); }}>Status aktualisieren</button></div>}
  </div>;
}
