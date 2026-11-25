import { ApiError, type Run, type RunStatus } from '@/api/client';

export const statusNames: Record<RunStatus, string> = {
  queued: '排队中',
  running: '运行中',
  awaiting_approval: '等待审批',
  awaiting_clarification: '等待澄清',
  cancelling: '正在取消',
  completed: '已完成',
  failed: '失败',
  cancelled: '已取消',
  interrupted: '已中断',
};

export const nativeStatuses: Record<string, string> = {
  ENQUEUED: '排队中',
  DELAYED: '延迟执行',
  PENDING: '待处理或运行中',
  SUCCESS: '执行成功',
  ERROR: '执行失败',
  CANCELLED: '已取消',
  MAX_RECOVERY_ATTEMPTS_EXCEEDED: '超出恢复次数',
};

export const roleNames: Record<string, string> = {
  public_signal: '公开舆情',
  internal_knowledge: '内部知识',
  risk_assessment: '风险评估',
  response_strategy: '应对策略',
};

const stageNames: Record<string, string> = {
  planning: '规划研究',
  plan: '规划研究',
  plan_research: '规划研究',
  enrich_query_images: '识别研究图片',
  awaiting_clarification: '需求澄清',
  awaiting_approval: 'Research Brief',
  revise_brief: 'AI 修改 Brief',
  revising_brief: 'AI 修改 Brief',
  research: '研究执行',
  research_review: 'Review · 证据复核',
  generate_final_report: '生成报告',
};

export function stageName(stage: string) {
  return stageNames[stage] ?? roleNames[stage.replace(/_agent$/, '')] ?? stage;
}

export function runStatus(run: Run) {
  return run.retry_requested ? '等待重试' : statusNames[run.status];
}

const dateTime = new Intl.DateTimeFormat('zh-CN', {
  year: 'numeric',
  month: '2-digit',
  day: '2-digit',
  hour: '2-digit',
  minute: '2-digit',
  hour12: false,
});

export function dateLabel(value?: string | number | null) {
  if (value == null || value === '') return '—';
  const date = new Date(value);
  return Number.isFinite(date.valueOf()) ? dateTime.format(date) : '—';
}

export function durationLabel(ms: number) {
  const seconds = Math.floor(Math.max(0, ms) / 1000);
  const minutes = Math.floor(seconds / 60);
  const hours = Math.floor(minutes / 60);
  if (hours) return hours + ' 小时 ' + (minutes % 60) + ' 分钟';
  if (minutes) return minutes + ' 分钟 ' + (seconds % 60) + ' 秒';
  return seconds + ' 秒';
}

export function sizeLabel(bytes: number) {
  if (bytes >= 1024 * 1024) return Number((bytes / (1024 * 1024)).toFixed(1)) + ' MB';
  if (bytes >= 1024) return Number((bytes / 1024).toFixed(1)) + ' KB';
  return bytes + ' B';
}

export function errorLabel(error: unknown) {
  if (error instanceof ApiError) {
    if (error.status === 401) return '访问令牌无效，请在系统设置中重新验证。';
    if (error.status === 404) return '未找到所请求的记录。';
    if (error.status === 413) return '请求超出服务端允许的大小。' + error.message;
    return error.message;
  }
  return error instanceof Error && error.name !== 'TypeError'
    ? error.message
    : '连接服务失败，请检查网络后再试。';
}

export function newKey() {
  return Array.from(crypto.getRandomValues(new Uint8Array(16)), (value) =>
    value.toString(16).padStart(2, '0'),
  ).join('');
}
