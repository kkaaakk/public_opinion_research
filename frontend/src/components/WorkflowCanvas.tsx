import { memo, useEffect, useMemo, useState } from 'react';
import {
  Background,
  Controls,
  Handle,
  MarkerType,
  Position,
  ReactFlow,
  ReactFlowProvider,
  useReactFlow,
  useNodesState,
  type Edge,
  type Node,
  type NodeProps,
} from '@xyflow/react';
import { Crosshair, GitBranch, List, Workflow } from 'lucide-react';
import { Badge } from './common';
import { EDGES, PHASES, type NodeStatus, type PhaseId, type Projection } from '../projection';

type StudioNode = Node<
  { label: string; detail: string; status: NodeStatus; instances: number },
  'studio'
>;
const WorkflowNode = memo(function WorkflowNode({ data, selected }: NodeProps<StudioNode>) {
  return (
    <div className={`workflow-node state-${data.status}${selected ? ' selected' : ''}`}>
      <Handle type="target" position={Position.Top} />
      <div className="node-title">
        <span className="node-dot" />
        <strong>{data.label}</strong>
        <span className="node-count">{data.instances || '—'}</span>
      </div>
      <div className="node-meta">
        <small>{data.detail}</small>
        <Badge status={data.status} />
      </div>
      <Handle type="source" position={Position.Bottom} />
    </div>
  );
});
const nodeTypes = { studio: WorkflowNode };
const positions: Record<PhaseId, { x: number; y: number }> = {
  intake: { x: 260, y: 0 },
  planning: { x: 260, y: 76 },
  approval: { x: 260, y: 152 },
  public_signal: { x: 80, y: 242 },
  internal_knowledge: { x: 440, y: 242 },
  review: { x: 260, y: 332 },
  risk_assessment: { x: 260, y: 408 },
  response_strategy: { x: 260, y: 484 },
  reporting: { x: 260, y: 560 },
};
function Canvas({
  projection,
  selected,
  onSelect,
}: {
  projection: Projection;
  selected: PhaseId | null;
  onSelect: (id: PhaseId) => void;
}) {
  const flow = useReactFlow<StudioNode>();
  const [follow, setFollow] = useState(false);
  const [list, setList] = useState(() => window.matchMedia('(max-width: 720px)').matches);
  const activeIds = PHASES.filter(
    (p) => projection.statuses[p.id] === 'running' || projection.statuses[p.id] === 'paused',
  )
    .map((p) => p.id)
    .join(',');
  useEffect(() => {
    if (follow && activeIds)
      void flow.fitView({
        nodes: activeIds.split(',').map((id) => ({ id })),
        padding: 0.7,
        maxZoom: 1,
        duration: 250,
      });
  }, [activeIds, follow, flow]);
  const [nodes, setNodes, onNodesChange] = useNodesState<StudioNode>(
    PHASES.map((p) => ({
      id: p.id,
      type: 'studio',
      position: positions[p.id],
      selected: selected === p.id,
      draggable: false,
      deletable: false,
      ariaLabel: `${p.label} · ${projection.statuses[p.id]}`,
      data: {
        label: p.label,
        detail: p.detail,
        status: projection.statuses[p.id],
        instances: projection.instances.filter((i) => i.phase === p.id).length,
      },
    })),
  );
  useEffect(() => {
    // React Flow reports real measured dimensions through onNodesChange.
    // Preserve them when observation facts change, or adoption hides the nodes.
    setNodes((current) =>
      current.map((node) => ({
        ...node,
        selected: node.id === selected,
        ariaLabel: `${node.data.label} · ${projection.statuses[node.id as PhaseId]}`,
        data: {
          ...node.data,
          status: projection.statuses[node.id as PhaseId],
          instances: projection.instances.filter((i) => i.phase === node.id).length,
        },
      })),
    );
  }, [projection, selected, setNodes]);
  const edges = useMemo<Edge[]>(() => {
    const result: Edge[] = EDGES.map(([source, target]) => ({
      id: `${source}-${target}`,
      source,
      target,
      type: 'smoothstep',
      markerEnd: { type: MarkerType.ArrowClosed },
      animated: projection.statuses[target] === 'running',
      style: { stroke: projection.statuses[source] === 'completed' ? '#4c8f78' : '#34414d' },
    }));
    for (const role of ['public_signal', 'internal_knowledge'] as const) {
      const observed = projection.followups.some((e) => !e.roles || e.roles.includes(role));
      result.push({
        id: `followup-${role}`,
        source: 'review',
        target: role,
        type: 'smoothstep',
        label: observed
          ? `Follow-up ×${projection.followups.filter((e) => !e.roles || e.roles.includes(role)).length}`
          : 'Follow-up',
        style: { stroke: observed ? '#c6a96e' : '#34414d', strokeDasharray: '5 5' },
        labelStyle: { fill: observed ? '#c6a96e' : '#8592a3', fontSize: 10 },
      });
    }
    return result;
  }, [projection]);
  return (
    <section className="panel flow-panel" aria-label="工作流拓扑">
      <div className="panel-head">
        <strong>
          <Workflow size={16} />
          业务工作流
        </strong>
        <div className="canvas-actions">
          <button
            className="text-button"
            aria-label="切换工作流列表"
            aria-pressed={list}
            onClick={() => setList((v) => !v)}
          >
            <List size={14} />
            {list ? '拓扑' : '列表'}
          </button>
          <button
            className={`text-button ${follow ? 'accent' : ''}`}
            onClick={() => setFollow((v) => !v)}
            aria-pressed={follow}
            disabled={list}
          >
            <Crosshair size={14} />
            自动定位{follow ? '开' : '关'}
          </button>
        </div>
      </div>
      {list ? (
        <div className="workflow-list">
          {PHASES.map((phase) => (
            <button
              key={phase.id}
              className={selected === phase.id ? 'chosen' : ''}
              onClick={() => onSelect(phase.id)}
            >
              <div>
                <strong>{phase.label}</strong>
                <Badge status={projection.statuses[phase.id]} />
              </div>
              <small>
                {phase.detail} · {projection.instances.filter((i) => i.phase === phase.id).length}{' '}
                个 Step 实例
              </small>
            </button>
          ))}
        </div>
      ) : (
        <div
          className="flow-frame"
          onKeyDown={(event) => {
            if (event.key !== 'Enter' && event.key !== ' ') return;
            const id =
              event.target instanceof Element
                ? event.target.closest('.react-flow__node')?.getAttribute('data-id')
                : null;
            if (id && PHASES.some((phase) => phase.id === id)) {
              event.preventDefault();
              onSelect(id as PhaseId);
            }
          }}
        >
          <ReactFlow<StudioNode>
            nodes={nodes}
            onNodesChange={onNodesChange}
            edges={edges}
            nodeTypes={nodeTypes}
            fitView
            fitViewOptions={{ padding: 0.16 }}
            minZoom={0.35}
            maxZoom={1.4}
            nodesConnectable={false}
            nodesDraggable={false}
            deleteKeyCode={null}
            onNodeClick={(_, node) => onSelect(node.id as PhaseId)}
            colorMode="dark"
            proOptions={{ hideAttribution: true }}
          >
            <Background color="#25303b" gap={20} />
            <Controls showInteractive={false} />
          </ReactFlow>
        </div>
      )}
      <div className="flow-legend">
        <span>
          <i />
          未执行
        </span>
        <span className="accent">
          <i />
          执行中
        </span>
        <span>
          <GitBranch size={13} />
          虚线为条件补查；节点角标为真实实例数
        </span>
      </div>
    </section>
  );
}
export function WorkflowCanvas(props: {
  projection: Projection;
  selected: PhaseId | null;
  onSelect: (id: PhaseId) => void;
}) {
  return (
    <ReactFlowProvider>
      <Canvas {...props} />
    </ReactFlowProvider>
  );
}
