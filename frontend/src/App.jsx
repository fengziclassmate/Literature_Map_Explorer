import { useEffect, useMemo, useRef, useState } from 'react';
import cytoscape from 'cytoscape';
import {
  BookOpen,
  Brain,
  CircleAlert,
  FileText,
  GitBranch,
  Loader2,
  Network,
  Play,
  RefreshCcw,
  Search,
  Sparkles,
} from 'lucide-react';

const API_BASE = import.meta.env.VITE_API_BASE || 'http://127.0.0.1:8000';

const initialSettings = {
  seed_doi: '10.1038/nature14539',
  max_depth_backward: 1,
  max_depth_forward: 1,
  max_papers_total: 40,
  per_paper_limit: 20,
};

const emptyGraph = { nodes: [], edges: [] };

async function request(path, options = {}) {
  const response = await fetch(`${API_BASE}${path}`, {
    headers: { 'Content-Type': 'application/json', ...(options.headers || {}) },
    ...options,
  });
  if (!response.ok) {
    const text = await response.text();
    throw new Error(text || `HTTP ${response.status}`);
  }
  const contentType = response.headers.get('content-type') || '';
  if (contentType.includes('application/json')) return response.json();
  return response.text();
}

function formatNumber(value) {
  if (value === null || value === undefined || value === '') return '—';
  return Number(value).toLocaleString('zh-CN');
}

function fieldValue(value) {
  return value || '暂无';
}

function App() {
  const [settings, setSettings] = useState(initialSettings);
  const [project, setProject] = useState(null);
  const [graph, setGraph] = useState(emptyGraph);
  const [paperCards, setPaperCards] = useState([]);
  const [selectedId, setSelectedId] = useState(null);
  const [report, setReport] = useState('');
  const [status, setStatus] = useState('待分析');
  const [error, setError] = useState('');
  const [isRunning, setIsRunning] = useState(false);
  const [isReporting, setIsReporting] = useState(false);
  const cyRef = useRef(null);
  const graphRef = useRef(null);

  const selectedCard = useMemo(() => {
    if (!selectedId) return paperCards[0] || null;
    return paperCards.find((card) => card.paper?.paper_key === selectedId) || null;
  }, [paperCards, selectedId]);

  useEffect(() => {
    if (!graphRef.current) return;
    if (cyRef.current) {
      cyRef.current.destroy();
      cyRef.current = null;
    }
    const elements = [
      ...(graph.nodes || []),
      ...(graph.edges || []),
    ];
    const cy = cytoscape({
      container: graphRef.current,
      elements,
      layout: {
        name: elements.length > 18 ? 'cose' : 'circle',
        animate: false,
        padding: 42,
        nodeRepulsion: 6500,
        idealEdgeLength: 120,
      },
      wheelSensitivity: 0.18,
      minZoom: 0.35,
      maxZoom: 2.2,
      style: [
        {
          selector: 'node',
          style: {
            width: 'mapData(citation_count, 0, 500, 26, 58)',
            height: 'mapData(citation_count, 0, 500, 26, 58)',
            'background-color': '#f7f8f6',
            'border-width': 2,
            'border-color': '#23443f',
            label: 'data(label)',
            color: '#1d1f1e',
            'font-size': 10,
            'font-family': 'serif',
            'text-wrap': 'wrap',
            'text-max-width': 112,
            'text-valign': 'bottom',
            'text-margin-y': 8,
          },
        },
        {
          selector: 'node:selected',
          style: {
            'background-color': '#b33b2e',
            'border-color': '#111413',
            color: '#111413',
          },
        },
        {
          selector: 'edge',
          style: {
            width: 1.4,
            'line-color': '#8ba39a',
            'target-arrow-color': '#8ba39a',
            'target-arrow-shape': 'triangle',
            'curve-style': 'bezier',
            opacity: 0.72,
          },
        },
      ],
    });
    cy.on('tap', 'node', (event) => setSelectedId(event.target.id()));
    cyRef.current = cy;
    return () => cy.destroy();
  }, [graph]);

  async function createProject() {
    setIsRunning(true);
    setError('');
    setStatus('正在解析 DOI 与抓取引用网络');
    setReport('');
    try {
      const created = await request('/projects', {
        method: 'POST',
        body: JSON.stringify(settings),
      });
      setProject(created);
      setStatus('正在载入网络与 Paper Cards');
      const [graphResponse, cardsResponse] = await Promise.all([
        request(`/graph/${created.project_id}`),
        request(`/projects/${created.project_id}/paper-cards`),
      ]);
      setGraph(graphResponse.elements || emptyGraph);
      setPaperCards(cardsResponse.paper_cards || []);
      setSelectedId((cardsResponse.paper_cards || [])[0]?.paper?.paper_key || null);
      setStatus('分析完成');
    } catch (err) {
      setError(err.message || String(err));
      setStatus('分析失败');
    } finally {
      setIsRunning(false);
    }
  }

  async function reloadProjectData() {
    if (!project?.project_id) return;
    setError('');
    setStatus('正在刷新当前项目');
    try {
      const [graphResponse, cardsResponse] = await Promise.all([
        request(`/graph/${project.project_id}`),
        request(`/projects/${project.project_id}/paper-cards`),
      ]);
      setGraph(graphResponse.elements || emptyGraph);
      setPaperCards(cardsResponse.paper_cards || []);
      setStatus('已刷新');
    } catch (err) {
      setError(err.message || String(err));
      setStatus('刷新失败');
    }
  }

  async function generateReport() {
    if (!project?.project_id) return;
    setIsReporting(true);
    setError('');
    setStatus('正在生成中文综述');
    try {
      const response = await request(`/projects/${project.project_id}/report`, { method: 'POST' });
      setReport(response.markdown || response.markdown_content || '');
      setStatus('综述已生成');
    } catch (err) {
      setError(err.message || String(err));
      setStatus('综述生成失败');
    } finally {
      setIsReporting(false);
    }
  }

  const nodeCount = graph.nodes?.length || 0;
  const edgeCount = graph.edges?.length || 0;

  return (
    <main className="workspace-shell">
      <section className="topbar">
        <div>
          <p className="eyebrow">中文文献地图工作台</p>
          <h1>Literature Map Explorer</h1>
        </div>
        <div className="status-strip">
          <span className={isRunning || isReporting ? 'pulse-dot busy' : 'pulse-dot'} />
          <span>{status}</span>
        </div>
      </section>

      <section className="workbench-grid">
        <aside className="control-rail" aria-label="项目参数">
          <div className="panel-title">
            <Search size={18} />
            <span>导入论文</span>
          </div>
          <label className="field-block">
            <span>种子论文 DOI</span>
            <input
              value={settings.seed_doi}
              onChange={(event) => setSettings({ ...settings, seed_doi: event.target.value })}
              placeholder="10.xxxx/xxxxx"
            />
          </label>
          <div className="settings-grid">
            <label>
              <span>参考层数</span>
              <input
                type="number"
                min="0"
                max="5"
                value={settings.max_depth_backward}
                onChange={(event) => setSettings({ ...settings, max_depth_backward: Number(event.target.value) })}
              />
            </label>
            <label>
              <span>被引层数</span>
              <input
                type="number"
                min="0"
                max="5"
                value={settings.max_depth_forward}
                onChange={(event) => setSettings({ ...settings, max_depth_forward: Number(event.target.value) })}
              />
            </label>
            <label>
              <span>论文上限</span>
              <input
                type="number"
                min="1"
                max="5000"
                value={settings.max_papers_total}
                onChange={(event) => setSettings({ ...settings, max_papers_total: Number(event.target.value) })}
              />
            </label>
            <label>
              <span>单篇扩展</span>
              <input
                type="number"
                min="1"
                max="200"
                value={settings.per_paper_limit}
                onChange={(event) => setSettings({ ...settings, per_paper_limit: Number(event.target.value) })}
              />
            </label>
          </div>
          <button className="primary-action" onClick={createProject} disabled={isRunning}>
            {isRunning ? <Loader2 className="spin" size={18} /> : <Play size={18} />}
            <span>{isRunning ? '运行中' : '开始分析'}</span>
          </button>
          <button className="secondary-action" onClick={reloadProjectData} disabled={!project || isRunning}>
            <RefreshCcw size={17} />
            <span>刷新项目</span>
          </button>

          <div className="metric-stack">
            <Metric icon={<BookOpen size={17} />} label="论文" value={nodeCount} />
            <Metric icon={<GitBranch size={17} />} label="引用边" value={edgeCount} />
            <Metric icon={<Brain size={17} />} label="卡片" value={paperCards.filter((card) => card.summary).length} />
          </div>

          {error && (
            <div className="error-box">
              <CircleAlert size={17} />
              <span>{error}</span>
            </div>
          )}
        </aside>

        <section className="map-stage" aria-label="引用网络">
          <div className="stage-toolbar">
            <div className="panel-title">
              <Network size={18} />
              <span>引用网络</span>
            </div>
            <span className="project-id">{project?.project_id || '尚未创建项目'}</span>
          </div>
          <div className="network-canvas" ref={graphRef}>
            {!nodeCount && <div className="empty-state">输入 DOI 后生成文献地图</div>}
          </div>
        </section>

        <aside className="paper-desk" aria-label="Paper Card">
          <PaperCard card={selectedCard} />
        </aside>
      </section>

      <section className="lower-grid">
        <div className="paper-list">
          <div className="panel-title">
            <BookOpen size={18} />
            <span>论文清单</span>
          </div>
          <div className="list-scroll">
            {paperCards.length === 0 && <p className="muted">暂无论文</p>}
            {paperCards.map((card) => (
              <button
                className={`paper-row ${selectedId === card.paper?.paper_key ? 'active' : ''}`}
                key={card.paper?.paper_key}
                onClick={() => setSelectedId(card.paper?.paper_key)}
              >
                <span>{card.paper?.title || 'Untitled'}</span>
                <small>{card.paper?.year || 'n.d.'} · {card.paper?.venue || '未知来源'}</small>
              </button>
            ))}
          </div>
        </div>

        <div className="report-panel">
          <div className="report-head">
            <div className="panel-title">
              <FileText size={18} />
              <span>领域综述</span>
            </div>
            <button className="report-action" onClick={generateReport} disabled={!project || isReporting}>
              {isReporting ? <Loader2 className="spin" size={17} /> : <Sparkles size={17} />}
              <span>{isReporting ? '生成中' : '生成综述'}</span>
            </button>
          </div>
          <article className="report-body">
            {report ? <MarkdownPreview markdown={report} /> : <p className="muted">Paper Cards 准备好后可生成中文综述。</p>}
          </article>
        </div>
      </section>
    </main>
  );
}

function Metric({ icon, label, value }) {
  return (
    <div className="metric-item">
      {icon}
      <span>{label}</span>
      <strong>{formatNumber(value)}</strong>
    </div>
  );
}

function PaperCard({ card }) {
  const paper = card?.paper;
  const summary = card?.summary;
  if (!paper) {
    return (
      <div className="paper-card empty-card">
        <div className="panel-title">
          <Brain size={18} />
          <span>Paper Card</span>
        </div>
        <p className="muted">选择网络节点后查看论文卡片。</p>
      </div>
    );
  }

  return (
    <div className="paper-card">
      <div className="panel-title">
        <Brain size={18} />
        <span>Paper Card</span>
      </div>
      <h2>{paper.title}</h2>
      <div className="paper-meta">
        <span>{paper.year || 'n.d.'}</span>
        <span>{paper.venue || '未知来源'}</span>
        <span>引用 {formatNumber(paper.citation_count)}</span>
      </div>
      <div className="score-line">
        <span>相关度 {formatNumber(summary?.relevance_score)}</span>
        <span>置信度 {formatNumber(summary?.summary_confidence)}</span>
        <span>{summary?.summary_level || '未总结'}</span>
      </div>
      <SummaryField label="一句话总结" value={summary?.one_sentence_summary} strong />
      <SummaryField label="研究问题" value={summary?.research_problem} />
      <SummaryField label="数据来源" value={summary?.data_sources} />
      <SummaryField label="方法" value={summary?.methods} />
      <SummaryField label="关键发现" value={summary?.key_findings} />
      <SummaryField label="贡献" value={summary?.contributions} />
      <SummaryField label="局限" value={summary?.limitations} />
      <SummaryField label="未来工作" value={summary?.future_work} />
      <SummaryField label="与种子论文关系" value={summary?.relation_to_seed} />
    </div>
  );
}

function SummaryField({ label, value, strong = false }) {
  return (
    <section className={`summary-field ${strong ? 'lead' : ''}`}>
      <h3>{label}</h3>
      <p>{fieldValue(value)}</p>
    </section>
  );
}

function MarkdownPreview({ markdown }) {
  return markdown.split('\n').map((line, index) => {
    if (line.startsWith('# ')) return <h2 key={index}>{line.replace(/^#\s+/, '')}</h2>;
    if (line.startsWith('## ')) return <h3 key={index}>{line.replace(/^##\s+/, '')}</h3>;
    if (line.startsWith('- ')) return <p className="report-bullet" key={index}>{line}</p>;
    if (!line.trim()) return <div className="report-gap" key={index} />;
    return <p key={index}>{line}</p>;
  });
}

export default App;

