import { lazy, Suspense, useCallback, useEffect, useState } from 'react';
import { api, getPlan, post } from './api.js';
import Setup, { initialConfig } from './Setup.jsx';
import Preview from './Preview.jsx';
import History from './History.jsx';
import Guide from './Guide.jsx';
import JobView from './JobView.jsx';
import PrivilegeControl from './Privileges.jsx';
import { Steps } from './components.jsx';
import { LayoutGrid, Files, Clock, BookOpen, LockKeyhole, CircleHelp, Folder, Merge, ScanFace, LoaderCircle, X, AlertTriangle } from './icons.js';

const Faces = lazy(() => import('./Faces.jsx'));

const navigation = [
  { id: 'classify', label: '整理工作台', Icon: LayoutGrid },
  { id: 'dedup', label: '重复文件', Icon: Files },
  { id: 'merge', label: '跨盘归并', Icon: Merge },
  { id: 'faces', label: '人脸索引', Icon: ScanFace },
  { id: 'history', label: '任务记录', Icon: Clock },
  { id: 'guide', label: '使用指南', Icon: BookOpen },
];

export default function App() {
  const [view, setView] = useState('workspace');
  const [stage, setStage] = useState('setup');
  const [config, setConfig] = useState(() => initialConfig('classify'));
  const [settings, setSettings] = useState(null);
  const [plan, setPlan] = useState(null);
  const [job, setJob] = useState(null);
  const [error, setError] = useState('');
  const [starting, setStarting] = useState(false);
  const [cancelling, setCancelling] = useState(false);
  const [historyRefresh, setHistoryRefresh] = useState(0);
  const [privileges, setPrivileges] = useState(null);
  const [faceBusy, setFaceBusy] = useState(false);
  const reportError = useCallback(message => setError(message), []);
  const active = faceBusy || (job && ['running', 'cancelling'].includes(job.status));

  useEffect(() => {
    const controller = new AbortController();
    Promise.allSettled([
      api('/api/config', { signal: controller.signal }),
      api('/api/privileges', { signal: controller.signal }),
    ]).then(([configResult, privilegeResult]) => {
      if (configResult.status === 'fulfilled') setSettings(configResult.value);
      else if (configResult.reason.name !== 'AbortError') setError(configResult.reason.message);
      if (privilegeResult.status === 'fulfilled') setPrivileges(privilegeResult.value);
    });
    return () => controller.abort();
  }, []);

  useEffect(() => {
    if (!job?.id) return;
    const controller = new AbortController();
    let timer;
    const poll = async () => {
      try {
        const result = await api(`/api/job?${new URLSearchParams({ id: job.id })}`, { signal: controller.signal });
        setJob(result);
        if (['running', 'cancelling'].includes(result.status)) { timer = setTimeout(poll, 600); return; }
        setCancelling(false);
        if (result.status === 'completed' && result.action === 'scan' && result.result?.plan_id) {
          const nextPlan = await getPlan(result.result.plan_id, 'items', 0, 100, controller.signal);
          setPlan(nextPlan);
          setStage('preview');
        } else { setStage('job'); setHistoryRefresh(value => value + 1); }
      } catch (reason) {
        if (reason.name !== 'AbortError') { setError(reason.message); timer = setTimeout(poll, 1800); }
      }
    };
    poll();
    return () => { controller.abort(); clearTimeout(timer); };
  }, [job?.id]);

  const startJob = async (action, payload) => {
    setStarting(true);
    setError('');
    try {
      const result = await post(`/api/${action}`, payload);
      setJob({ id: result.job_id, action, status: 'running', progress: {} });
      setStage('job');
      setView('workspace');
    } finally { setStarting(false); }
  };
  const scan = async () => {
    try { await startJob('scan', config); } catch (reason) { setError(reason.message); }
  };
  const cancel = async () => {
    setCancelling(true);
    try { await post('/api/cancel', { job_id: job.id }); setJob(current => ({ ...current, status: 'cancelling' })); }
    catch (reason) { setError(reason.message); setCancelling(false); }
  };
  const changeMode = next => {
    setConfig(current => ({ ...current, mode: next }));
    setView('workspace');
    setStage('setup');
    setPlan(null);
    setError('');
  };
  const navigate = id => {
    if (active || starting) return;
    if (id === 'history' || id === 'guide' || id === 'faces') { setView(id); setError(''); }
    else changeMode(id);
  };
  const current = view === 'workspace' ? config.mode : view;
  const title = view === 'history' ? '每一份文件，都有来路' : view === 'guide' ? '轻松上手，安心归序' : view === 'faces' ? '熟悉的面孔，一眼找到' : config.mode === 'dedup' ? '相同的内容，留一份就好' : config.mode === 'merge' ? '汇聚文件，各归其位' : '整理文件，从容有序';
  const subtitle = view === 'history' ? '查看整理结果，让每一步操作清晰可查。' : view === 'guide' ? '了解整理方式与保护机制，让重要内容一直在。' : view === 'faces' ? '在本机识别相似人脸，建立只属于你的照片索引。' : config.mode === 'dedup' ? '精确识别完全相同的文件，先看清，再安全隔离。' : config.mode === 'merge' ? '把分散在不同文件夹、不同磁盘的内容，归并到一个位置。' : '将散落的文件分类归档，找出重复，保留每一份重要内容。';
  const elevate = async () => {
    const result = await post('/api/privileges/elevate', {});
    if (!result.launched) throw new Error(result.message || '未能启动管理员会话。');
    return result;
  };

  return <div className="app-shell"><aside className="sidebar"><a href="#" className="brand" aria-label="归序 File Atlas 首页" onClick={event => { event.preventDefault(); navigate('classify'); }}><span className="brand-mark" aria-hidden="true"><Folder /><Folder /><Folder /></span><span><strong>归序</strong><small>File Atlas</small></span></a><nav aria-label="主要导航">{navigation.map(({ id, label, Icon }) => <button key={id} aria-label={label} className={`nav-item ${current === id ? 'active' : ''}`} aria-current={current === id ? 'page' : undefined} onClick={() => navigate(id)} disabled={Boolean(active || starting)}><Icon size={22} strokeWidth={1.7} /><span>{label}</span></button>)}</nav><div className="sidebar-bottom"><div className="local-status"><LockKeyhole size={19} /><span>本地处理 · 文件不上传</span></div><PrivilegeControl status={privileges} onElevate={elevate} />{settings && <small>File Atlas {settings.version}</small>}</div></aside>
    <main className="main-content"><header className="page-header"><div><h1>{title}</h1><p>{subtitle}</p></div><button className="button help-button secondary" onClick={() => navigate('guide')} disabled={Boolean(active || starting)}><CircleHelp size={19} />帮助</button></header>
      {error && <div className="error-banner" role="alert"><AlertTriangle size={19} /><span>{error}</span><button className="icon-button" onClick={() => setError('')} aria-label="关闭提示"><X size={17} /></button></div>}
      {view === 'workspace' ? <><Steps step={stage === 'setup' ? 1 : stage === 'preview' || (stage === 'job' && job?.action === 'scan') ? 2 : 3} />{stage === 'setup' ? <Setup config={config} setConfig={setConfig} onScan={scan} onModeChange={changeMode} onError={reportError} busy={starting} privileges={privileges} onElevate={elevate} /> : stage === 'preview' && plan ? <Preview key={plan.id} plan={plan} onBack={() => { setStage('setup'); setPlan(null); }} onExecute={(planId, selectedIds, excludedIds) => startJob('execute', { plan_id: planId, selected_ids: selectedIds, excluded_ids: excludedIds, confirmed: true })} onError={reportError} /> : job ? <JobView job={job} cancelling={cancelling} onCancel={cancel} onNew={() => { setStage('setup'); setPlan(null); setError(''); }} onHistory={() => setView('history')} /> : null}</> : view === 'history' ? <History onRestore={taskId => startJob('restore', { task_id: taskId })} onError={reportError} refresh={historyRefresh} dataDir={settings?.data_dir} /> : view === 'faces' ? <Suspense fallback={<div className="face-loading"><LoaderCircle className="spin" size={30} /><span>正在打开人脸索引…</span></div>}><Faces onBusyChange={setFaceBusy} onError={reportError} /></Suspense> : <Guide />}
    </main>
  </div>;
}
