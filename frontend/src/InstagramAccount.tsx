import { useCallback, useEffect, useRef, useState } from 'react';
import type { ChangeEvent, FormEvent, MouseEvent } from 'react';
import { Api } from './api';
import type { InstagramAccountStatus, InstagramAction } from './api';
import { Icon } from './icons';
import { readTotpQr } from './totpQr';

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
  const [totpSecret, setTotpSecret] = useState('');
  const [totpBusy, setTotpBusy] = useState<'save' | 'remove' | 'scan' | null>(null);
  const [totpMessage, setTotpMessage] = useState('');
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
  const qrFile = useRef<HTMLInputElement>(null);
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
  useEffect(() => { if (!account?.configured) { setTotpSecret(''); setTotpMessage(''); } }, [account?.configured]);
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
    setBusy(true); setError(''); setTotpMessage('');
    const submittedUsername = username.trim(); const submittedPassword = password;
    setPassword(''); if (kind === 'save') setUsername('');
    if (kind !== 'login') setTotpSecret('');
    try {
      const next = kind === 'save' ? await api.saveInstagramAccount(submittedUsername, submittedPassword) : kind === 'login' ? await api.loginInstagramAccount() : await api.removeInstagramAccount();
      if (mounted.current) { setAccount(next); if (kind === 'remove') { setUsername(''); setPassword(''); setTotpSecret(''); setLiveOpen(false); setText(''); } }
    } catch {
      if (mounted.current) setError(kind === 'remove' ? 'Der Zugang konnte nicht entfernt werden. Bitte versuche es erneut.' : 'Die Anmeldung konnte nicht gestartet werden. Prüfe den Zugang oder versuche es erneut.');
    } finally { pending.current = false; if (mounted.current) setBusy(false); }
  }
  async function importTotpQr(event: ChangeEvent<HTMLInputElement>) {
    const file = event.currentTarget.files?.[0];
    event.currentTarget.value = '';
    if (!file || pending.current || !account?.configured) return;
    pending.current = true; operation.current++; setTotpBusy('scan'); setTotpSecret(''); setError(''); setTotpMessage('');
    try {
      const secret = await readTotpQr(file);
      if (!mounted.current) return;
      const next = await api.saveInstagramTotp(secret);
      if (mounted.current) { setAccount(next); setTotpMessage('Der Einrichtungsschlüssel aus dem QR-Bild wurde hinterlegt. Es wurde keine neue Anmeldung gestartet.'); }
    } catch {
      if (mounted.current) setError('Das QR-Bild konnte nicht übernommen werden. Wähle ein klares PNG-, JPEG- oder WebP-Bild bis 5 MiB und 16 Megapixel mit genau einem Instagram-Eintrag.');
    } finally { pending.current = false; if (mounted.current) setTotpBusy(null); }
  }
  async function changeTotp(kind: 'save' | 'remove', event?: FormEvent) {
    event?.preventDefault();
    if (pending.current || !account?.configured || (kind === 'save' && !totpSecret.trim())) return;
    const submittedSecret = totpSecret.trim();
    setTotpSecret(''); setError(''); setTotpMessage('');
    if (kind === 'save' && /^\d{6}$/.test(submittedSecret.replace(/\s/g, ''))) {
      setError('Verwende den einmaligen Instagram-Einrichtungsschlüssel, keinen sechsstelligen Bestätigungscode.');
      return;
    }
    pending.current = true; operation.current++; setTotpBusy(kind);
    try {
      const next = kind === 'save' ? await api.saveInstagramTotp(submittedSecret) : await api.removeInstagramTotp();
      if (mounted.current) {
        setAccount(next);
        setTotpMessage(kind === 'save' ? 'Der Einrichtungsschlüssel wurde hinterlegt. Es wurde keine neue Anmeldung gestartet.' : 'Der Einrichtungsschlüssel wurde entfernt. Zugang und Sitzung bleiben bestehen.');
      }
    } catch {
      if (mounted.current) setError(kind === 'save' ? 'Der Einrichtungsschlüssel konnte nicht gespeichert werden. Prüfe die Base32-Zeichenfolge oder den otpauth-Link und versuche es erneut.' : 'Der Einrichtungsschlüssel konnte nicht entfernt werden. Bitte versuche es erneut.');
    } finally { pending.current = false; if (mounted.current) setTotpBusy(null); }
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
  const mutationBusy = busy || actionBusy || Boolean(totpBusy);
  const totpConfigured = Boolean(account?.totp_configured);
  const credentialsForm = <form className="ig-credentials" onSubmit={event => void change('save', event)} autoComplete="off"><fieldset disabled={mutationBusy}><div className="ig-fields"><div><label htmlFor="ig-username">Instagram-Benutzername</label><input id="ig-username" name="instagram-username" autoComplete="off" autoCapitalize="none" spellCheck={false} value={username} onChange={event => setUsername(event.target.value)} required /></div><div><label htmlFor="ig-password">Instagram-Passwort</label><input id="ig-password" name="instagram-password" type="password" autoComplete="off" value={password} onChange={event => setPassword(event.target.value)} required /></div></div><button className="button primary" disabled={!username.trim() || !password || mutationBusy}>{busy ? <span className="spinner" /> : <Icon name="link" size={17} />}Speichern und anmelden</button></fieldset></form>;
  return <div className="ig-account" aria-labelledby="ig-account-heading"><div className="section-heading"><div><h3 id="ig-account-heading">Instagram verbinden</h3><p className="source-intro">Vela meldet sich direkt bei Instagram an. Nur wenn Instagram eine Bestätigung verlangt, greifst du hier ein.</p></div><span className={`ig-account-state ${account?.state === 'ready' ? 'good-text' : account?.state === 'needs_attention' || account?.state === 'error' ? 'warning-text' : ''}`} role="status">{loading ? 'Wird geladen …' : status}</span></div>
    {account?.message && <p className="ig-status-message">{account.message}</p>}
    {!loading && (account?.configured ? <><div className="ig-account-actions"><button className="button secondary" onClick={() => void change('login')} disabled={mutationBusy || account.state === 'logging_in'}>{busy || account.state === 'logging_in' ? <span className="spinner" /> : <Icon name="retry" size={17} />}{account.state === 'error' || account.state === 'needs_attention' ? 'Erneut versuchen' : 'Anmelden'}</button><button className="button ghost" onClick={() => void change('remove')} disabled={mutationBusy}>Zugang entfernen</button></div><p className="source-note">„Zugang entfernen“ löscht auch das Browserprofil und die Instagram-Sitzung.</p><details className="ig-replace"><summary>Zugang ersetzen</summary>{credentialsForm}</details></> : credentialsForm)}
    {account?.browser_available && <div className="ig-confirmation"><button className={`button ${account.state === 'needs_attention' ? 'primary' : 'secondary'}`} onClick={() => { setLiveOpen(!liveOpen); setZoomed(false); setText(''); }} aria-expanded={liveOpen} aria-controls="ig-live-browser"><Icon name="eye" size={17} />{liveOpen ? 'Bestätigung schließen' : 'Bestätigung öffnen'}</button>{!liveOpen && <p className="source-note">Das Livebild wird erst beim Öffnen geladen.</p>}</div>}
    {liveOpen && account?.browser_available && <div id="ig-live-browser" className="ig-live-browser"><div className="ig-live-heading"><h4>Bestätigung bei Instagram</h4><button className="text-button" onClick={() => setZoomed(!zoomed)}>{zoomed ? 'An Breite anpassen' : 'Bild vergrößern'}</button></div><p className="source-note">Klicke im Livebild auf das gewünschte Feld. Gib danach Text oder einen Bestätigungscode ein. Mit „Tab“ kannst du Felder auch ohne Maus auswählen.</p><div className="ig-screen-scroll">{screenUrl ? <button className={`ig-screen-target ${zoomed ? 'zoomed' : ''}`} type="button" onClick={clickImage} disabled={mutationBusy || !screenReady || !account.interaction_id} aria-label="Im Instagram-Browser klicken; Eingabetaste sendet Enter"><img ref={image} src={screenUrl} alt="Aktuelles Livebild des Instagram-Browsers" onLoad={() => setScreenReady(true)} draggable={false} /></button> : <div className="source-loading" role="status"><span className="spinner" />Livebild wird geladen …</div>}</div>{screenError && <p className="source-note warning-text" role="status">{screenError}</p>}<form className="ig-text-input" onSubmit={event => { event.preventDefault(); if (text) void send({ kind: 'text', text }); }}><label htmlFor="ig-browser-text">Text oder Bestätigungscode</label><div><input id="ig-browser-text" value={text} onChange={event => setText(event.target.value)} autoComplete="off" spellCheck={false} maxLength={4096} disabled={mutationBusy || !account.interaction_id} /><button className="button primary" disabled={!text || mutationBusy || !account.interaction_id}>{actionBusy ? <span className="spinner" /> : <Icon name="arrow" size={17} />}Text eingeben</button></div></form><div className="ig-browser-keys" role="group" aria-label="Tasten im Instagram-Browser">{keys.map(item => <button className="button secondary" key={item.key} type="button" aria-label={`Taste ${item.key} senden`} onClick={() => void send({ kind: 'key', key: item.key })} disabled={mutationBusy || !account.interaction_id}>{item.label}</button>)}</div><div className="ig-scroll-actions"><button className="button ghost" onClick={() => void send({ kind: 'scroll', delta: -500 })} disabled={mutationBusy || !account.interaction_id}>Nach oben scrollen</button><button className="button ghost" onClick={() => void send({ kind: 'scroll', delta: 500 })} disabled={mutationBusy || !account.interaction_id}>Nach unten scrollen</button></div><p className="ig-privacy-note">Livebilder werden hier nicht gespeichert. Hinterlegte Zugänge stehen Downloads im vertrauenswürdigen Heimnetz zur Verfügung.</p></div>}
    {!loading && <section className="ig-totp" aria-labelledby="ig-totp-heading">
      <div className="ig-live-heading"><h4 id="ig-totp-heading">Authenticator-Schlüssel <span>(optional)</span></h4><span className={`ig-account-state ${totpConfigured ? 'good-text' : ''}`} role="status" aria-label={`Authenticator-Schlüssel: ${totpConfigured ? 'Hinterlegt' : 'Nicht hinterlegt'}`}>{totpConfigured ? 'Hinterlegt' : 'Nicht hinterlegt'}</span></div>
      <p className="source-note" id="ig-totp-help">Verwende den einmaligen Einrichtungsschlüssel von Instagram als Base32-Zeichenfolge oder otpauth-Link, keinen sechsstelligen Bestätigungscode.</p>
      {!account?.configured && <p className="source-note">Hinterlege zuerst deinen Instagram-Zugang.</p>}
      <form className="ig-totp-form" autoComplete="off" onSubmit={event => void changeTotp('save', event)}>
        <fieldset disabled={mutationBusy || !account?.configured}><label htmlFor="ig-totp-secret">Instagram-Einrichtungsschlüssel</label><div className="ig-totp-row"><input id="ig-totp-secret" name="instagram-setup-secret" type="password" autoComplete="off" autoCapitalize="none" spellCheck={false} maxLength={4096} value={totpSecret} onChange={event => setTotpSecret(event.target.value)} aria-describedby="ig-totp-help ig-totp-privacy" /><button className="button secondary" type="submit" disabled={!totpSecret.trim() || mutationBusy || !account?.configured}>{totpBusy === 'save' ? <span className="spinner" /> : <Icon name="check" size={17} />}{totpConfigured ? 'Schlüssel ersetzen' : 'Schlüssel speichern'}</button></div></fieldset>
      </form>
      <div className="ig-totp-qr"><input ref={qrFile} type="file" accept="image/png,image/jpeg,image/webp" className="sr-only" tabIndex={-1} aria-label="QR-Bild für Instagram auswählen" onChange={event => void importTotpQr(event)} disabled={mutationBusy || !account?.configured} /><button className="button secondary" type="button" disabled={mutationBusy || !account?.configured} onClick={() => qrFile.current?.click()}>{totpBusy === 'scan' ? <span className="spinner" /> : <Icon name="folder" size={17} />}QR-Bild importieren</button><span className="source-file-label">PNG, JPEG oder WebP · bis 5 MiB und 16 Megapixel</span></div>
      {totpConfigured && <button className="button ghost ig-totp-remove" type="button" disabled={mutationBusy || !account?.configured} onClick={() => void changeTotp('remove')}>{totpBusy === 'remove' ? <span className="spinner" /> : <Icon name="close" size={17} />}Schlüssel entfernen</button>}
      <p className="source-note">Speichern startet keine Anmeldung. Nutze anschließend bei Bedarf „Anmelden“. Vela erzeugt nur dann einen Code, wenn Instagram einen App-Code verlangt. Weitere Bestätigungen können nötig bleiben.</p>
      <details className="ig-totp-instructions"><summary>Welcher Schlüssel ist gemeint?</summary><p>Nutze den Einrichtungsschlüssel, den Instagram beim Einrichten einer Authenticator-App für dieses Konto bereitstellt. Ein aktueller Code aus Google Authenticator, einer E-Mail oder einer Nachricht ist dafür nicht geeignet.</p><p id="ig-totp-privacy">Der Schlüssel wird nach dem Senden aus dem Eingabefeld geleert und hier nicht wieder angezeigt. Teile ihn nicht im Chat. „Schlüssel entfernen“ lässt deinen Instagram-Zugang und die bestehende Sitzung erhalten.</p></details>
      <details className="ig-totp-instructions"><summary>Vorhandenen Google-Authenticator-Eintrag übernehmen</summary><p>Öffne in Google Authenticator „Codes übertragen“ → „Codes exportieren“ und wähle ausschließlich den einen Instagram-Eintrag. Erstelle ein Foto oder einen Screenshot des angezeigten QR-Codes und wähle das Bild hier aus.</p><p>Das Bild bleibt in deinem Browser. Nur der gelesene Einrichtungsinhalt wird direkt an Vela auf deinem Heimserver gesendet. Es gibt keine Bildvorschau. Teile weder Bild noch Einrichtungsschlüssel im Chat.</p></details>
      {totpMessage && <div className="notice success" role="status"><Icon name="check" size={18} /><span>{totpMessage}</span></div>}
    </section>}
    {(error || statusError) && <div className="notice error" role="alert"><Icon name="alert" size={18} /><span>{error || statusError}</span><button className="text-button" onClick={() => { setError(''); void loadStatus(); }}>Status aktualisieren</button></div>}
  </div>;
}
