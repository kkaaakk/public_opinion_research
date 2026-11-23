import { useState, type FormEvent } from 'react';
import { Play } from 'lucide-react';
import type { RunRequest } from '../protocol';
import { Modal } from './common';
export function CreateDialog({
  onClose,
  onCreate,
  busy,
}: {
  onClose: () => void;
  onCreate: (request: RunRequest) => void;
  busy: boolean;
}) {
  const [request, setRequest] = useState<RunRequest>({
    topic: '',
    org_context: '',
    rag_enabled: false,
  });
  function submit(e: FormEvent) {
    e.preventDefault();
    if (request.topic.trim()) {
      onCreate({
        topic: request.topic.trim(),
        org_context: request.org_context ?? '',
        rag_enabled: request.rag_enabled === true,
      });
    }
  }
  return (
    <Modal title="新建研究" onClose={onClose}>
      <form className="research-form" onSubmit={submit}>
        <label>
          研究主题
          <textarea
            autoFocus
            required
            maxLength={12000}
            rows={4}
            value={request.topic}
            onChange={(e) => setRequest({ ...request, topic: e.target.value })}
            placeholder="描述要调查的品牌、企业、产品或舆情事件"
          />
        </label>
        <label>
          组织背景（可选）
          <textarea
            rows={2}
            maxLength={8000}
            value={request.org_context ?? ''}
            onChange={(e) => setRequest({ ...request, org_context: e.target.value })}
          />
        </label>
        <div className="form-checks">
          <label>
            <input
              type="checkbox"
              checked={request.rag_enabled === true}
              onChange={(e) => setRequest({ ...request, rag_enabled: e.target.checked })}
            />
            RAG 内部知识检索
          </label>
        </div>
        <p className="note">
          模型、研究深度、检索方式、预算与审批策略由服务端 config.yaml 管理，不随单次研究改变。
          时间范围与重点关注问题请直接写进研究主题。
        </p>
        <div className="form-actions">
          <button type="button" className="button" onClick={onClose}>
            关闭
          </button>
          <button className="button primary" disabled={busy || !request.topic.trim()}>
            <Play size={14} />
            创建持久 Run
          </button>
        </div>
      </form>
    </Modal>
  );
}
