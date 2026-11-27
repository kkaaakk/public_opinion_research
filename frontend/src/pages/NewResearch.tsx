import { useQuery, useQueryClient } from '@tanstack/react-query';
import { ArrowRight, ImagePlus, X } from 'lucide-react';
import { useEffect, useRef, useState, type FormEvent } from 'react';
import { useNavigate, useSearchParams } from 'react-router';
import { useApi } from '@/api/auth';
import { ApiError, type Run, type Schema } from '@/api/client';
import { AccessRequired, ErrorNotice, LinkButton, Loading } from '@/components/shared';
import { Button } from '@/components/ui/button';
import { Switch } from '@/components/ui/switch';
import { Textarea } from '@/components/ui/textarea';
import {
  clearSubmission,
  readSubmission,
  saveSubmission,
  submissionBody,
  type PendingSubmission,
  type SelectedImage,
} from '@/features/submission';
import { newKey, sizeLabel } from '@/lib/presentation';

function ImagePreview({ image, remove }: { image: SelectedImage; remove?: () => void }) {
  const [url, setUrl] = useState('');
  useEffect(() => {
    const objectUrl = URL.createObjectURL(image.file);
    setUrl(objectUrl);
    return () => URL.revokeObjectURL(objectUrl);
  }, [image.file]);
  return (
    <div className="upload-preview">
      {url && <img src={url} alt={image.file.name} />}
      <span title={image.file.name}>{image.file.name}</span>
      {remove && (
        <Button
          type="button"
          size="icon-xs"
          variant="ghost"
          aria-label={'移除 ' + image.file.name}
          onClick={remove}
        >
          <X />
        </Button>
      )}
    </div>
  );
}

function mediaType(file: File, limits: Schema['ImageLimits']) {
  const inferred = /\.jpe?g$/i.test(file.name)
    ? 'image/jpeg'
    : /\.png$/i.test(file.name)
      ? 'image/png'
      : /\.webp$/i.test(file.name)
        ? 'image/webp'
        : '';
  return limits.media_types.find((type) => type === (file.type || inferred));
}

function ResearchForm({
  settings,
  sessionId,
  previousRequest,
}: {
  settings: Schema['Settings'];
  sessionId: string | null;
  previousRequest?: Schema['RequestSummary'];
}) {
  const { api } = useApi();
  const client = useQueryClient();
  const navigate = useNavigate();
  const [topic, setTopic] = useState(previousRequest?.topic ?? '');
  const [context, setContext] = useState(previousRequest?.org_context ?? '');
  const [rag, setRag] = useState(previousRequest?.rag_enabled ?? settings.new_run_rag_default);
  const [files, setFiles] = useState<SelectedImage[]>([]);
  const [pending, setPending] = useState<PendingSubmission | null>(null);
  const [restoring, setRestoring] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>();
  const [dragging, setDragging] = useState(false);
  const input = useRef<HTMLInputElement>(null);
  const sending = useRef(false);
  const mounted = useRef(false);
  const limits = settings.image_limits;
  const targetSession = pending ? (pending.request.research_session_id ?? null) : sessionId;
  const session = useQuery({
    queryKey: ['session', targetSession, 0],
    queryFn: ({ signal }) => api.session(targetSession!, 0, signal),
    enabled: Boolean(targetSession),
    refetchInterval: (query) => (query.state.data?.can_continue === false ? 10_000 : false),
  });

  useEffect(() => {
    mounted.current = true;
    let active = true;
    async function restore() {
      const draft = await readSubmission();
      if (!draft || !active) return;
      setPending(draft);
      setTopic(draft.request.topic);
      setContext(draft.request.org_context ?? '');
      setRag(draft.request.rag_enabled ?? settings.new_run_rag_default);
      setFiles(draft.files);
      try {
        const run = await api.submission(draft.key);
        if (active) {
          await clearSubmission();
          if (active) navigate('/runs/' + run.run_id, { replace: true });
        }
      } catch (failure) {
        if (active && !(failure instanceof ApiError && failure.status === 404)) setError(failure);
      }
    }
    void restore()
      .catch(() => {
        if (active) setError('浏览器无法保存待确认的提交。');
      })
      .finally(() => {
        if (active) setRestoring(false);
      });
    return () => {
      active = false;
      mounted.current = false;
    };
  }, [api, navigate, settings.new_run_rag_default]);

  function addFiles(selection: FileList | File[]) {
    setDragging(false);
    if (!limits.enabled || pending || busy) return;
    setError(undefined);
    const next = [...files];
    for (const file of Array.from(selection)) {
      const type = mediaType(file, limits);
      if (next.length >= limits.max_images) {
        setError('最多可添加 ' + limits.max_images + ' 张图片。');
        break;
      }
      if (!type) {
        setError('不支持该图片类型 · ' + file.name);
        continue;
      }
      if (!file.size || file.size > limits.max_bytes || file.name.length > 180) {
        setError('图片需非空且不超过 ' + sizeLabel(limits.max_bytes) + '，文件名最多 180 个字符。');
        continue;
      }
      next.push({ id: newKey(), file, mediaType: type });
    }
    setFiles(next);
    if (input.current) input.current.value = '';
  }

  async function confirm(run: Run) {
    if (!mounted.current) return;
    await clearSubmission();
    void client.invalidateQueries({ queryKey: ['sessions'] });
    if (run.research_session_id)
      void client.invalidateQueries({ queryKey: ['session', run.research_session_id] });
    if (mounted.current) navigate('/runs/' + run.run_id);
  }

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (sending.current || restoring || !topic.trim()) return;
    const draft = pending ?? {
      key: newKey(),
      request: {
        topic: topic.trim(),
        org_context: context.trim(),
        rag_enabled: rag,
        research_session_id: sessionId,
      },
      files,
    };
    sending.current = true;
    setBusy(true);
    setError(undefined);
    try {
      await saveSubmission(draft);
      setPending(draft);
      const body = await submissionBody(draft);
      await confirm(await api.create(body, draft.key));
    } catch (failure) {
      if (failure instanceof ApiError && [400, 409, 413, 415, 422].includes(failure.status)) {
        // An earlier attempt may have been admitted before config or availability changed.
        try {
          await confirm(await api.submission(draft.key));
          return;
        } catch (lookupError) {
          if (lookupError instanceof ApiError && lookupError.status === 404) {
            await clearSubmission();
            setPending(null);
          }
        }
      }
      if (mounted.current) setError(failure);
    } finally {
      sending.current = false;
      if (mounted.current) setBusy(false);
    }
  }

  async function checkReceipt() {
    if (!pending || sending.current) return;
    sending.current = true;
    setBusy(true);
    setError(undefined);
    try {
      await confirm(await api.submission(pending.key));
    } catch (failure) {
      setError(
        failure instanceof ApiError && failure.status === 404
          ? '服务端尚未找到该提交，可以重试同一请求。'
          : failure,
      );
    } finally {
      sending.current = false;
      if (mounted.current) setBusy(false);
    }
  }

  const frozen = busy || restoring || Boolean(pending);
  const activeSession = session.data?.can_continue === false;
  return (
    <>
      {targetSession && (
        <p className="continue-label">
          <LinkButton to={'/sessions/' + targetSession} variant="secondary">
            返回研究会话
          </LinkButton>
          {session.data?.items[0]?.topic && (
            <span title={session.data.items[0].topic}>{session.data.items[0].topic}</span>
          )}
        </p>
      )}
      {error !== undefined && <ErrorNotice error={error} />}
      {session.isError && !pending && (
        <ErrorNotice error={session.error} retry={() => void session.refetch()} />
      )}
      <form
        className="research-form"
        data-dragging={dragging}
        onSubmit={(event) => void submit(event)}
        onDragOver={(event) => {
          if (limits.enabled && Array.from(event.dataTransfer.types).includes('Files')) {
            event.preventDefault();
            if (!frozen) setDragging(true);
          }
        }}
        onDragLeave={(event) => {
          if (!event.relatedTarget || !event.currentTarget.contains(event.relatedTarget as Node))
            setDragging(false);
        }}
        onDrop={(event) => {
          event.preventDefault();
          addFiles(event.dataTransfer.files);
        }}
      >
        <div className="field">
          <label htmlFor="research-topic">
            研究主题 <span className="required-mark">*</span>
          </label>
          <Textarea
            id="research-topic"
            placeholder="输入想研究的议题、时间范围或关注对象"
            required
            rows={4}
            maxLength={12_000}
            disabled={frozen}
            value={topic}
            onChange={(event) => setTopic(event.target.value)}
          />
        </div>
        <div className="field">
          <label htmlFor="organization">
            企业背景 <span>选填</span>
          </label>
          <Textarea
            id="organization"
            placeholder="提供与这次研究有关的企业背景或业务约束"
            rows={3}
            maxLength={8000}
            disabled={frozen}
            value={context}
            onChange={(event) => setContext(event.target.value)}
          />
        </div>
        {limits.enabled && (
          <div className="image-field">
            <input
              ref={input}
              type="file"
              multiple
              hidden
              accept={limits.media_types.join(',')}
              onChange={(event) => event.target.files && addFiles(event.target.files)}
            />
            <Button
              type="button"
              variant="outline"
              disabled={frozen}
              onClick={() => input.current?.click()}
            >
              <ImagePlus data-icon="inline-start" />
              添加图片
            </Button>
            <span className="muted text-xs">
              可拖到这里 · 最多 {limits.max_images} 张 · 每张 {sizeLabel(limits.max_bytes)}
            </span>
          </div>
        )}
        {files.length > 0 && (
          <div className="upload-list">
            {files.map((image) => (
              <ImagePreview
                key={image.id}
                image={image}
                remove={
                  frozen
                    ? undefined
                    : () => setFiles((items) => items.filter((item) => item.id !== image.id))
                }
              />
            ))}
          </div>
        )}
        <div className="form-footer">
          <div className="rag-switch">
            <Switch id="enable-rag" checked={rag} onCheckedChange={setRag} disabled={frozen} />
            <label htmlFor="enable-rag">
              企业知识库<span className="muted"> · {rag ? '已启用' : '已关闭'}</span>
            </label>
          </div>
          <div className="flex flex-wrap items-center gap-2">
            {pending && (
              <Button
                type="button"
                variant="outline"
                disabled={busy || restoring}
                onClick={() => void checkReceipt()}
              >
                查询提交结果
              </Button>
            )}
            <Button
              type="submit"
              size="lg"
              disabled={
                busy ||
                restoring ||
                !topic.trim() ||
                (!pending &&
                  Boolean(sessionId) &&
                  (session.isPending || session.isError || activeSession))
              }
            >
              {busy ? '提交中…' : pending ? '重试同一提交' : '开始研究'}
              <ArrowRight data-icon="inline-end" />
            </Button>
          </div>
        </div>
        {activeSession && !pending && <p className="muted">会话中仍有未结束或待处理的研究。</p>}
        {pending && !busy && (
          <p className="muted">此提交尚未确认，可查询结果或使用同一请求重试。</p>
        )}
      </form>
    </>
  );
}

export default function NewResearch() {
  const { api, authenticated } = useApi();
  const [params] = useSearchParams();
  const previousRunId = params.get('fromRun');
  const settings = useQuery({
    queryKey: ['settings'],
    queryFn: ({ signal }) => api.settings(signal),
    enabled: authenticated,
  });
  const previousRun = useQuery({
    queryKey: ['run', previousRunId],
    queryFn: ({ signal }) => api.run(previousRunId!, signal),
    enabled: authenticated && Boolean(previousRunId),
  });
  const sessionId = previousRunId
    ? previousRun.data?.research_session_id || null
    : params.get('session');
  return (
    <div className="new-research">
      <div className="new-intro">
        <h1>{sessionId || previousRunId ? '延续研究会话' : '开始一项研究'}</h1>
        <p>
          {previousRun.data
            ? '已带入原研究信息，点击开始研究会创建一条新的 Run。'
            : '从一个问题开始，逐步明确研究范围并沉淀可追溯的报告。'}
        </p>
      </div>
      {!authenticated ? (
        <AccessRequired />
      ) : settings.isError ? (
        <ErrorNotice error={settings.error} retry={() => void settings.refetch()} />
      ) : previousRunId && previousRun.isError ? (
        <ErrorNotice error={previousRun.error} retry={() => void previousRun.refetch()} />
      ) : previousRunId && !previousRun.data ? (
        <Loading text="正在读取原研究信息…" />
      ) : previousRunId && !sessionId ? (
        <ErrorNotice error="原研究的会话不可用。" />
      ) : settings.data ? (
        <ResearchForm
          key={previousRunId ?? sessionId ?? ''}
          settings={settings.data}
          sessionId={sessionId}
          previousRequest={previousRun.data?.request}
        />
      ) : (
        <Loading text="正在读取研究输入限制…" />
      )}
    </div>
  );
}
