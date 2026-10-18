import { number } from './api.js';
import { Notice } from './components.jsx';
import { CheckCircle2, LoaderCircle, ShieldCheck, Clock, ArrowRight, AlertTriangle } from './icons.js';

export default function JobView({ job, onCancel, onNew, onHistory, cancelling }) {
  const progress = job.progress || {};
  const active = ['running', 'cancelling'].includes(job.status);
  const current = progress.current ?? progress.completed ?? progress.processed ?? 0;
  const total = progress.total || 0;
  const ratio = total > 0 ? Math.min(100, Math.round(current / total * 100)) : null;
  const result = job.result || {};
  const cancelled = job.status === 'cancelled';
  const failed = job.status === 'failed';
  const partial = result.failed > 0 || result.status === 'partial' || result.status === 'restore_partial';
  const action = job.action === 'scan' ? '扫描' : job.action === 'restore' ? '恢复' : '整理';
  const title = active ? (job.status === 'cancelling' ? '正在安全停止…' : `正在${action}，请稍候`) : failed ? `这次${action}未完成` : cancelled ? `已停止${action}` : partial ? `已完成可安全${action}的文件` : `${action}完成，井然有序`;
  return <section className="panel job-view" aria-live="polite"><div className={`job-symbol ${failed || partial ? 'job-warning' : ''}`}>{active ? <LoaderCircle size={43} className="spin" /> : failed || partial ? <AlertTriangle size={43} /> : cancelled ? <ShieldCheck size={43} /> : <CheckCircle2 size={43} />}</div><h2>{title}</h2><p>{active ? job.status === 'cancelling' ? '正在处理的文件会先安全结束。请保持程序打开。' : job.action === 'scan' ? '只读取文件，暂时不会修改任何内容。' : '逐个验证并处理文件，请保持程序打开。' : cancelled ? '已完成的操作仍然有效，可在任务记录中查看或恢复。' : failed ? '请查看下方提示。已记录的操作可以从任务记录中检查。' : job.action === 'restore' ? '可以在任务记录中查看本次恢复的详细结果。' : '所有已完成操作均记录在本机，可以随时查看。'}</p>
    {active && <div className="job-progress"><div className={`progress-track ${ratio === null ? 'indeterminate' : ''}`} role="progressbar" aria-label={`${action}进度`} aria-valuemin={0} aria-valuemax={100} aria-valuenow={ratio ?? undefined}><span style={ratio === null ? undefined : { width: `${ratio}%` }} /></div><div className="progress-caption"><span>{typeof progress.message === 'string' ? progress.message : '正在检查文件…'}</span><strong>{total > 0 ? `${number(current)} / ${number(total)}` : current > 0 ? `${number(current)} 个文件` : '准备中'}</strong></div>{(progress.path || progress.source) && <p className="progress-path" title={progress.path || progress.source}>{progress.path || progress.source}</p>}</div>}
    {!active && !failed && result.total !== undefined && <div className="result-counts"><div><strong>{number(result.completed)}</strong><span>{job.action === 'restore' ? '原任务已完成' : '完成文件'}</span></div>{job.action === 'restore' && <div><strong>{number(result.restored)}</strong><span>已恢复文件</span></div>}<div><strong>{number(result.failed)}</strong><span>未完成文件</span></div></div>}
    {job.error && <Notice type="error"><p>{job.error}</p></Notice>}
    {result.errors?.length > 0 && <details className="job-errors"><summary>查看 {number(result.errors.length)} 条结果提示</summary><ul>{result.errors.map((error, index) => <li key={index}><span>{error.path || error.source}</span>{error.error || error.reason || String(error)}</li>)}</ul></details>}
    <div className="job-actions">{active ? <button className="button secondary" onClick={onCancel} disabled={cancelling || job.status === 'cancelling'}>{cancelling || job.status === 'cancelling' ? <LoaderCircle className="spin" size={17} /> : null}{job.status === 'cancelling' ? '正在停止' : '取消任务'}</button> : <><button className="button secondary" onClick={onNew}>继续整理<ArrowRight size={17} /></button><button className="button primary" onClick={onHistory}><Clock size={17} />查看任务记录</button></>}</div>
  </section>;
}
