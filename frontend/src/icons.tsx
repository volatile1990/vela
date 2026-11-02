import type { CSSProperties } from 'react';

export type IconName = 'download' | 'queue' | 'history' | 'server' | 'arrow' | 'link' | 'video' | 'audio' | 'check' | 'close' | 'retry' | 'external' | 'eye' | 'folder' | 'clock' | 'alert' | 'chevron' | 'refresh' | 'film' | 'clipboard';
const paths: Record<IconName, string> = {
  download: 'M12 3v12m-5-5 5 5 5-5M4 16v4h16v-4',
  queue: 'M4 6h16M4 12h11M4 18h7m7-4v7m-3-3 3 3 3-3',
  history: 'M3 11a9 9 0 1 1 2.5 7M3 4v7h7m2-5v6l4 2',
  server: 'M4 3h16v7H4zM4 14h16v7H4zM7 6.5h.01M7 17.5h.01m4-11h6m-6 11h6',
  arrow: 'M4 12h16m-6-6 6 6-6 6', link: 'm10 14 4-4m-6 6-1.5 1.5a4 4 0 0 1-5.6-5.6L6 8.3a4 4 0 0 1 5.6 0m.8 7.4a4 4 0 0 0 5.6 0l3.1-3.1a4 4 0 0 0-5.6-5.6L14 8.5',
  video: 'm9 5 11 7-11 7zM4 5v14', audio: 'M9 17V5l11-2v12M9 9l11-2M9 17a3 3 0 1 1-3-3c1.7 0 3 1.3 3 3Zm11-2a3 3 0 1 1-3-3c1.7 0 3 1.3 3 3Z',
  check: 'm5 12 4 4L19 6', close: 'm6 6 12 12M6 18 18 6', retry: 'M20 7v5h-5m5 0a8 8 0 1 0-1.8 5',
  external: 'M14 3h7v7m0-7L10 14m-2-9H3v16h16v-5',
  eye: 'M2 12s4-7 10-7 10 7 10 7-4 7-10 7S2 12 2 12Zm13 0a3 3 0 1 1-6 0 3 3 0 0 1 6 0Z',
  folder: 'M3 5h7l2 3h9v12H3z', clock: 'M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0Zm-9-5v5l3 2',
  alert: 'm12 3 10 18H2zM12 9v5m0 3h.01', chevron: 'm8 5 7 7-7 7', refresh: 'M20 4v6h-6M4 20v-6h6m10-4a8 8 0 0 0-14-4M4 14a8 8 0 0 0 14 4',
  film: 'M3 4h18v16H3zM7 4v16M17 4v16M3 8h4m-4 8h4M17 8h4m-4 8h4',
  clipboard: 'M9 4H5v17h14V4h-4M9 2h6v5H9z',
};
export function Icon({ name, size = 20, className = '', style }: { name: IconName; size?: number; className?: string; style?: CSSProperties }) {
  return <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.65" strokeLinecap="round" strokeLinejoin="round" className={className} style={style} aria-hidden="true"><path d={paths[name]} /></svg>;
}
export function VelaMark({ size = 40 }: { size?: number }) {
  return <svg width={size} height={size} viewBox="0 0 48 48" fill="none" aria-hidden="true"><rect width="48" height="48" rx="13" fill="#ffb093" fillOpacity=".11" /><path d="M12 13 23 37 36 10 24 27Z" fill="#ffb093" /><path d="m24 27 12-17-4 24Z" fill="#d98064" /></svg>;
}
