import { useCallback, useEffect, useState } from 'react';
import { api, modeLabel, actionLabel, number } from './api.js';
import { Modal, Notice } from './components.jsx';
import { Clock, RefreshCw, RotateCcw, LoaderCircle, Folder, AlertTriangle } from './icons.js';

const statusLabels = { completed: '已完成', partial: '部分完成', cancelled: '已取消', restored: '已恢复', restore_partial: '部分恢复', restore_cancelled: '恢复已取消', recoverable: '可继续恢复', invalid_journal: '任务记录不可读取', running: '执行中' };
function dateLabel(value) {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value || '时间未记录' : date.toLocaleString('zh-CN', { hour12: false });
}

export default function History({ onRestore, onError, refresh = 0, dataDir }) {
  const [tasks, setTasks] = useState([]);
  const [loading, setLoading] = useState(true);
  const [failed, setFailed] = useState(false);
  const [confirm, setConfirm] = useState(null);
  const [preparing, setPreparing] = useState(false);
  const [acknowledged, setAcknowledged] = useState(false);
  const load = useCallback(async () => {
    setLoading(true);
    try {
      const result = await api('/api/history');
      setTasks(result.tasks || []);
      setFailed(false);
    } catch (error) { onError(error.message); setFailed(true); } finally { setLoading(false); }
  }, [onError]);
  useEffect(() => { load(); }, [load, refresh]);
  const restore = async () => {
    setPreparing(true);
    try { await onRestore(confirm.task_id || confirm.id); setConfirm(null); }
    catch (error) { setConfirm(null); onError(error.message); } finally { setPreparing(false); }
  };

  return <div className="history-view"><div className="section-toolbar"><div><h2>每一次整理，都有迹可循</h2><p>任务记录保存在本机，可查看结果并恢复移动或隔离的文件。</p></div><button className="button secondary" onClick={load} disabled={loading}><RefreshCw size={17} className={loading ? 'spin' : ''} />刷新记录</button></div>
    <Notice title="恢复前会再次核验文件"><p>移动或隔离的文件可恢复原位置；复制任务可撤销生成的副本。遇到冲突或内容变化时，归序会跳过并保留现状。</p></Notice>
    <section className="panel history-panel">{loading ? <div className="empty-state"><LoaderCircle size={30} className="spin" /><p>正在读取本地任务记录…</p></div> : failed ? <div className="empty-state"><AlertTriangle size={35} /><h3>暂时无法读取任务记录</h3><p>请确认本地程序仍在运行，然后点击刷新记录。</p></div> : tasks.length === 0 ? <div className="empty-state"><Clock size={38} /><h3>还没有整理记录</h3><p>完成第一次整理后，可以在这里查看结果和恢复文件。</p></div> : <div className="history-list">{tasks.map(task => <article className="history-task" key={task.id || task.task_id}><div className="task-icon"><Folder size={23} /></div><div className="task-content"><div className="task-heading"><h3>{modeLabel(task.mode)}</h3><span className={`status-tag status-${task.status}`}>{statusLabels[task.status] || task.status}</span></div><p>{dateLabel(task.created_at)}<span className="dot">·</span>{actionLabel(task.operation || (task.mode === 'dedup' ? 'quarantine' : 'copy'))}</p><div className="task-counts"><span>完成 <strong>{number(task.completed)}</strong> / {number(task.total)}</span>{Boolean(task.failed) && <span className="warning-text">未完成 {number(task.failed)}</span>}{Boolean(task.restored) && <span>已恢复 {number(task.restored)}</span>}</div>{task.errors?.length > 0 && <details className="task-errors"><summary>查看 {number(task.errors.length)} 条提示</summary><ul>{task.errors.map((error, i) => <li key={i}><span>{error.path || error.source}</span>{error.error || error.reason || String(error)}</li>)}</ul></details>}<small className="task-id">任务 {task.task_id || task.id}</small></div>{task.restorable && <button className="button secondary restore-button" onClick={() => { setConfirm(task); setAcknowledged(false); }}><RotateCcw size={16} />{task.operation === 'copy' ? '撤销复制' : '恢复文件'}</button>}</article>)}</div>}</section>
    {dataDir && <p className="history-location"><Clock size={15} />本地记录位置：<span>{dataDir}</span></p>}
    {confirm && <Modal title={confirm.operation === 'copy' ? '撤销这次复制' : '恢复这次整理的文件'} onClose={() => setConfirm(null)} busy={preparing} footer={<><button className="button secondary" disabled={preparing} onClick={() => setConfirm(null)}>取消</button><button className="button primary" disabled={preparing || !acknowledged} onClick={restore}>{preparing ? <LoaderCircle size={17} className="spin" /> : <RotateCcw size={17} />}{confirm.operation === 'copy' ? '确认撤销复制' : '确认恢复'}</button></>}>
      {confirm.operation === 'copy' ? <><p>将删除本次任务在目标位置生成的副本，原文件保留。</p><p>归序会同时核验原文件和目标副本。只有原文件完整且未变、目标副本也未被修改时，才移除目标副本；否则跳过并保留文件。</p></> : <><p>将根据本地任务记录，把本次移动或隔离的文件送回原位置。</p><p>恢复只处理内容仍与任务记录一致的文件。同名冲突会跳过，不会覆盖现有文件。</p></>}
      <div className="confirm-destination"><Clock size={18} /><span>{modeLabel(confirm.mode)} · {dateLabel(confirm.created_at)}</span></div><label className="acknowledge"><input type="checkbox" checked={acknowledged} onChange={event => setAcknowledged(event.target.checked)} />{confirm.operation === 'copy' ? '我同意移除本次生成且核验无误的目标副本。' : '我同意根据这次任务记录恢复文件。'}</label></Modal>}
  </div>;
}
