export type Profile = 'original' | 'compatible' | 'audio';
export type JobStatus = 'queued' | 'probing' | 'downloading' | 'postprocessing' | 'finalizing' | 'waiting_storage' | 'interrupted' | 'completed' | 'failed' | 'cancelled';
export interface ServerInfo { version: string }
export interface Artifact { id: string; job_id: string; name: string; size: number; mime: string; relative_path: string }
export interface MetadataEntry { title?: string; id?: string; duration?: number; url?: string }
export interface Job {
  id: string; url: string; kind: 'probe' | 'download'; profile: Profile; max_height: number | null;
  playlist: boolean; playlist_items: string | null; status: JobStatus; phase: string | null;
  progress: number | null; downloaded_bytes: number | null; total_bytes: number | null;
  speed: number | null; eta: number | null; title: string | null; extractor: string | null;
  metadata: { title?: string; duration?: number; entries?: MetadataEntry[]; extractor?: string; media_id?: string; webpage_url?: string; [key: string]: unknown };
  error: string | null; error_code: string | null; cancel_requested: boolean; attempt: number;
  created_at: string; updated_at: string; finished_at: string | null; artifacts?: Artifact[];
}
export interface Storage { available?: boolean; free_bytes?: number; total_bytes?: number; path?: string; label?: string; error?: string }
export interface Overview {
  version: string; counts: { queued: number; active: number; completed: number; failed: number };
  worker: { heartbeat_at?: string; status?: string; engine?: string | Record<string, unknown>; storage?: Storage };
  storage?: Storage;
}
export interface DownloadOptions { url: string; profile: Profile; max_height: number | null; playlist: boolean; playlist_items: string | null }

export class ApiError extends Error {
  constructor(message: string, public status: number) { super(message); }
}

function errorMessage(body: unknown, status: number): string {
  if (status === 401) return 'Die Anfrage ist nicht erlaubt. Bitte lade die Seite neu.';
  if (status === 429) return 'Zu viele Versuche. Bitte warte einen Moment und versuche es erneut.';
  if (status === 422) return 'Bitte prüfe den Link und die gewählten Optionen.';
  if (status === 403) return 'Die Aktion konnte nicht bestätigt werden. Bitte lade die Seite neu.';
  if (body && typeof body === 'object' && 'detail' in body && typeof body.detail === 'string') return body.detail;
  return 'Die Anfrage konnte nicht abgeschlossen werden. Bitte versuche es erneut.';
}

export class Api {
  async request<T>(path: string, body?: unknown, extraHeaders?: Record<string, string>): Promise<T> {
    let response: Response;
    try {
      response = await fetch(`/api${path}`, {
        method: body === undefined ? 'GET' : 'POST', credentials: 'same-origin', cache: 'no-store',
        headers: {
          Accept: 'application/json',
          ...(body !== undefined ? { 'Content-Type': 'application/json' } : {}),
          ...extraHeaders,
        },
        ...(body !== undefined ? { body: JSON.stringify(body) } : {}),
      });
    } catch { throw new ApiError('Vela ist gerade nicht erreichbar. Prüfe deine Verbindung und versuche es erneut.', 0); }
    const result: unknown = await response.json().catch(() => null);
    if (!response.ok) throw new ApiError(errorMessage(result, response.status), response.status);
    return result as T;
  }

  serverInfo = () => this.request<ServerInfo>('/session');
  overview = () => this.request<Overview>('/overview');
  jobs = (offset = 0) => this.request<{ items: Job[]; total: number }>(`/jobs?limit=100&offset=${offset}`);
  job = (id: string) => this.request<Job>(`/jobs/${encodeURIComponent(id)}`);
  create = (options: DownloadOptions, key: string) => this.request<Job>('/jobs', options, { 'Idempotency-Key': key });
  preview = (options: DownloadOptions) => this.request<Job>('/previews', options);
  cancel = (id: string) => this.request<Job>(`/jobs/${encodeURIComponent(id)}/cancel`, {});
  retry = (id: string) => this.request<Job>(`/jobs/${encodeURIComponent(id)}/retry`, {});
}

export const terminal = (status: JobStatus) => ['completed', 'failed', 'cancelled'].includes(status);
export const active = (status: JobStatus) => !terminal(status);

export function validMediaUrl(value: string): boolean {
  try { const url = new URL(value); return ['http:', 'https:'].includes(url.protocol) && Boolean(url.hostname) && !url.username && !url.password; }
  catch { return false; }
}

export function formatBytes(value: number | null | undefined): string {
  if (value == null || !Number.isFinite(value)) return '–';
  if (value === 0) return '0 B';
  const index = Math.min(4, Math.max(0, Math.floor(Math.log(Math.max(value, 1)) / Math.log(1024))));
  return `${new Intl.NumberFormat('de-DE', { maximumFractionDigits: index > 0 ? 1 : 0 }).format(value / 1024 ** index)} ${['B', 'KB', 'MB', 'GB', 'TB'][index]}`;
}

export function formatDuration(value: number | null | undefined): string {
  if (value == null || !Number.isFinite(value)) return '–';
  const rounded = Math.max(0, Math.round(value));
  const seconds = String(rounded % 60).padStart(2, '0');
  if (rounded >= 3600) return `${Math.floor(rounded / 3600)}:${String(Math.floor(rounded / 60) % 60).padStart(2, '0')}:${seconds}`;
  return `${Math.floor(rounded / 60)}:${seconds}`;
}

export function hostLabel(value: string): string {
  try { return new URL(value).hostname.replace(/^www\./, ''); } catch { return 'Medienquelle'; }
}

export const statusLabels: Record<JobStatus, string> = {
  queued: 'Wartet', probing: 'Wird geprüft', downloading: 'Lädt herunter', postprocessing: 'Wird verarbeitet',
  finalizing: 'Wird gespeichert', waiting_storage: 'Wartet auf Speicher', interrupted: 'Unterbrochen',
  completed: 'Abgeschlossen', failed: 'Fehlgeschlagen', cancelled: 'Abgebrochen',
};
