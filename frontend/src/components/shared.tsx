import { ArrowLeft, ChevronLeft, ChevronRight, CircleAlert, KeyRound } from 'lucide-react';
import type { ReactNode } from 'react';
import { Link } from 'react-router';
import { ApiError, type RunStatus } from '@/api/client';
import { Alert, AlertDescription } from '@/components/ui/alert';
import { Badge } from '@/components/ui/badge';
import { Button, buttonVariants } from '@/components/ui/button';
import { Skeleton } from '@/components/ui/skeleton';
import { errorLabel, statusNames } from '@/lib/presentation';
import { cn } from '@/lib/utils';

export function LinkButton({
  to,
  children,
  className,
  variant = 'outline',
}: {
  to: string;
  children: ReactNode;
  className?: string;
  variant?: 'outline' | 'ghost' | 'default' | 'secondary' | 'link';
}) {
  return (
    <Link className={cn(buttonVariants({ variant }), className)} to={to}>
      {children}
    </Link>
  );
}

export function Status({ status, label }: { status: RunStatus; label?: string }) {
  const tone =
    status === 'completed'
      ? 'success'
      : status === 'failed' || status === 'interrupted'
        ? 'error'
        : status === 'cancelled' || status === 'queued'
          ? 'quiet'
          : status.startsWith('awaiting') || status === 'cancelling'
            ? 'wait'
            : 'active';
  return (
    <Badge variant="secondary" className="status" data-tone={tone}>
      <span className="status-dot" aria-hidden="true" />
      {label ?? statusNames[status]}
    </Badge>
  );
}

export function PageTitle({
  title,
  back,
  children,
}: {
  title: string;
  back?: string;
  children?: ReactNode;
}) {
  return (
    <header className="page-title">
      <div className="flex min-w-0 items-center gap-3">
        {back && (
          <LinkButton variant="ghost" to={back}>
            <ArrowLeft data-icon="inline-start" />
            返回
          </LinkButton>
        )}
        <h1>{title}</h1>
      </div>
      {children}
    </header>
  );
}

export function Loading({ text = '正在读取…' }: { text?: string }) {
  return (
    <div className="loading" role="status">
      <Skeleton className="h-3 w-24" />
      <span>{text}</span>
    </div>
  );
}

export function ErrorNotice({ error, retry }: { error: unknown; retry?: () => void }) {
  return (
    <Alert variant="destructive">
      <CircleAlert />
      <AlertDescription className="flex flex-wrap items-center justify-between gap-2">
        <span>{typeof error === 'string' ? error : errorLabel(error)}</span>
        {error instanceof ApiError && error.status === 401 ? (
          <LinkButton to="/settings">打开系统设置</LinkButton>
        ) : (
          retry && (
            <Button variant="outline" onClick={retry}>
              重新读取
            </Button>
          )
        )}
      </AlertDescription>
    </Alert>
  );
}

export function AccessRequired() {
  return (
    <div className="empty-content">
      <p>请验证 Web API 访问令牌以使用工作台。</p>
      <LinkButton to="/settings">
        <KeyRound data-icon="inline-start" />
        打开系统设置
      </LinkButton>
    </div>
  );
}

export function Pagination({
  label,
  previous,
  next,
  disabled = false,
}: {
  label: string;
  previous?: () => void;
  next?: () => void;
  disabled?: boolean;
}) {
  if (!previous && !next) return null;
  return (
    <nav className="pagination" aria-label="分页">
      <Button variant="outline" size="sm" disabled={!previous || disabled} onClick={previous}>
        <ChevronLeft data-icon="inline-start" />
        上一页
      </Button>
      <span>{label}</span>
      <Button variant="outline" size="sm" disabled={!next || disabled} onClick={next}>
        下一页
        <ChevronRight data-icon="inline-end" />
      </Button>
    </nav>
  );
}
