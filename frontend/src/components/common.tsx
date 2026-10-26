import { useEffect, useRef, type ReactNode } from 'react';
import { X } from 'lucide-react';
import { STATUS_LABELS } from '../projection';

export function time(value?: string | number | null): string {
  if (value == null || value === '') return '未提供';
  const d = new Date(value);
  return Number.isNaN(d.valueOf()) ? '未提供' : d.toLocaleString('zh-CN', { hour12: false });
}
export function duration(ms?: number): string {
  if (ms === undefined || !Number.isFinite(ms)) return '未提供';
  if (ms < 1000) return `${Math.round(ms)} ms`;
  if (ms < 60000) return `${(ms / 1000).toFixed(2)} s`;
  return `${Math.floor(ms / 60000)}m ${Math.round((ms % 60000) / 1000)}s`;
}
export function count(value?: number): string {
  return value === undefined ? '—' : value.toLocaleString('zh-CN');
}
export function Badge({ status }: { status?: string }) {
  return (
    <span className={`badge tone-${status ?? 'unknown'}`}>
      <i />
      {STATUS_LABELS[status ?? ''] ?? status ?? '未提供'}
    </span>
  );
}
export function Empty({ children }: { children: ReactNode }) {
  return <div className="empty">{children}</div>;
}
export function ErrorNote({ error }: { error?: string }) {
  return error ? (
    <p className="error-note" role="alert">
      {error}
    </p>
  ) : null;
}
export function JsonDetail({ value }: { value: unknown }) {
  return <pre className="json-detail">{JSON.stringify(value, null, 2)}</pre>;
}
export function Modal({
  title,
  onClose,
  children,
}: {
  title: string;
  onClose: () => void;
  children: ReactNode;
}) {
  const ref = useRef<HTMLDialogElement>(null);
  useEffect(() => {
    ref.current?.showModal();
  }, []);
  return (
    <dialog
      ref={ref}
      onCancel={(e) => {
        e.preventDefault();
        onClose();
      }}
      aria-label={title}
    >
      <div className="panel-head">
        <strong>{title}</strong>
        <button className="icon-button" aria-label="关闭" onClick={onClose}>
          <X size={17} />
        </button>
      </div>
      {children}
    </dialog>
  );
}
export function exportMarkdown(content: string, name = 'research-report') {
  const url = URL.createObjectURL(new Blob([content], { type: 'text/markdown;charset=utf-8' }));
  const a = document.createElement('a');
  a.href = url;
  a.download = `${name}.md`;
  a.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
