import { useEffect, useState } from 'react';
import { getPlan, exportPlan, bytes, number, basename, actionLabel } from './api.js';
import { Checkbox, Modal, Notice } from './components.jsx';
import MediaPreview, { MediaPreviewButton } from './MediaPreview.jsx';
import { ArrowLeft, ArrowRight, CheckCircle2, ChevronLeft, ChevronRight, Download, File, Files, Folder, LoaderCircle, ShieldCheck } from './preview-icons.js';

const categoryLabels = { picture: '图片', video: '视频', audio: '音频', document: '文档', duplicate: '重复副本', merged: '归并文件' };
// Keep aligned with the server's image/video formats. Ambiguous .ts needs the
// scan's video category (MPEG-TS sync bytes), rather than a filename guess.
const mediaExtension = /\.(?:3fr|ari|arw|bay|cr2|cr3|crw|dcr|dng|erf|fff|iiq|k25|kdc|mdc|mef|mos|mrw|nef|nrw|orf|pef|ptx|pxn|raf|raw|rwl|rw2|rwz|sr2|srf|srw|x3f|avif|bmp|exr|gif|hdr|heic|heif|ico|jp2|jpe?g|jpe|jfif|pgm|png|ppm|psd|tga|tiff?|webp|3gp|3g2|asf|avi|f4v|flv|m2ts|m4v|mkv|mov|mp4|mpe|mpeg|mpg|mts|mxf|ogv|r3d|rm|rmvb|vob|webm|wmv)$/i;

export default function Preview({ plan, onBack, onExecute, onError }) {
  const [kind, setKind] = useState('items');
  const [offset, setOffset] = useState(0);
  const [data, setData] = useState(plan);
  const [loading, setLoading] = useState(false);
  const [readError, setReadError] = useState('');
  const [reload, setReload] = useState(0);
  const [selection, setSelection] = useState({ all: true, ids: new Set() });
  const [confirm, setConfirm] = useState(false);
  const [acknowledged, setAcknowledged] = useState(false);
  const [preparing, setPreparing] = useState(false);
  const [exporting, setExporting] = useState(false);
  const [mediaRequest, setMediaRequest] = useState(null);
  const counts = plan.counts || {};
  const totalItems = counts.items || 0;
  const summary = plan.summary || {};
  const selectedCount = selection.all ? totalItems - selection.ids.size : selection.ids.size;
  const mode = plan.config?.mode || 'classify';
  const operation = mode === 'dedup' ? 'quarantine' : plan.config?.operation || 'copy';
  const destructive = operation !== 'copy';

  useEffect(() => {
    const controller = new AbortController();
    setLoading(true);
    setReadError('');
    getPlan(plan.id, kind, offset, 100, controller.signal)
      .then(setData)
      .catch(error => { if (error.name !== 'AbortError') { onError(error.message); setReadError(error.message); setData({ rows: [], total: 0 }); } })
      .finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, [plan.id, kind, offset, onError, reload]);

  const isSelected = id => selection.all ? !selection.ids.has(id) : selection.ids.has(id);
  const select = (ids, checked) => setSelection(current => {
    const next = new Set(current.ids);
    ids.forEach(id => (current.all ? !checked : checked) ? next.add(id) : next.delete(id));
    return { ...current, ids: next };
  });
  const pageIds = kind === 'items' ? (data.rows || []).map(row => row.id) : [];
  const pageSelected = pageIds.filter(isSelected).length;
  const changeKind = next => { setKind(next); setOffset(0); };
  const download = async () => {
    setExporting(true);
    try { await exportPlan(plan.id); } catch (error) { onError(error.message); } finally { setExporting(false); }
  };
  const execute = async () => {
    setPreparing(true);
    try {
      const selectedIds = selection.all ? null : Array.from(selection.ids);
      const excludedIds = selection.all ? Array.from(selection.ids) : [];
      await onExecute(plan.id, selectedIds, excludedIds);
      setConfirm(false);
    } catch (error) { setConfirm(false); onError(error.message); } finally { setPreparing(false); }
  };
  const metrics = [
    { label: '扫描文件', value: number(summary.files), detail: bytes(summary.bytes) },
    { label: '待整理文件', value: number(totalItems), detail: `${actionLabel(operation)}后保留完整内容` },
    mode === 'dedup' ? { label: '完全相同的副本', value: number(summary.duplicate_files), detail: `${number(summary.duplicate_groups)} 组重复文件` } : { label: '整理方式', value: actionLabel(operation), detail: operation === 'copy' ? '原文件完整保留' : '验证目标后移除原文件' },
    { label: mode === 'dedup' ? '可隔离的重复内容' : '已安全跳过', value: mode === 'dedup' ? bytes(summary.reclaimable) : number(counts.skipped), detail: mode === 'dedup' ? '隔离期间仍占用磁盘空间' : '查看下方的跳过清单' },
  ];

  return <div className="preview-view">
    <div className="section-toolbar"><div><h2>先看看，文件将如何归位</h2><p>预览不会修改文件。取消勾选，即可排除暂时不想处理的文件。</p></div><button className="button secondary" onClick={download} disabled={exporting}>{exporting ? <LoaderCircle size={17} className="spin" /> : <Download size={17} />}导出清单</button></div>
    <div className="metrics">{metrics.map(metric => <div className="metric" key={metric.label}><span>{metric.label}</span><strong>{metric.value}</strong><small>{metric.detail}</small></div>)}</div>
    {(plan.cancelled || plan.warnings?.length > 0) && <Notice type="warning" title={plan.cancelled ? '本次扫描已取消，请重新扫描后再整理。' : '请留意以下情况'}><ul>{(plan.warnings || []).map((warning, index) => <li key={index}>{typeof warning === 'string' ? warning : warning.message || JSON.stringify(warning)}</li>)}</ul></Notice>}
    <section className="panel preview-panel">
      <div className="table-tabs" role="tablist" aria-label="预览清单">{[{ id: 'items', label: '整理清单' }, ...(mode === 'dedup' ? [{ id: 'duplicates', label: '重复文件组' }] : []), { id: 'skipped', label: '跳过清单' }].map(tab => <button key={tab.id} role="tab" aria-selected={kind === tab.id} className={kind === tab.id ? 'active' : ''} onClick={() => changeKind(tab.id)}>{tab.label}<span>{number(counts[tab.id])}</span></button>)}</div>
      {kind === 'items' && <div className="selection-toolbar"><span>已选择 <strong>{number(selectedCount)}</strong> / {number(totalItems)} 个文件</span><div><button className="text-button" onClick={() => setSelection({ all: true, ids: new Set() })}>全部选中</button><button className="text-button" onClick={() => setSelection({ all: false, ids: new Set() })}>清空选择</button></div></div>}
      <div className={`preview-content ${loading ? 'is-loading' : ''}`} aria-busy={loading}>
        {loading && <div className="table-loading"><LoaderCircle className="spin" size={23} /><span>正在读取清单…</span></div>}
        {readError ? <div className="empty-state"><h3>暂时无法读取这页清单</h3><p>{readError}</p><button type="button" className="button secondary" onClick={() => setReload(value => value + 1)}>重新读取</button></div> : !loading && data.rows?.length === 0 ? <div className="empty-state"><CheckCircle2 size={37} /><h3>{kind === 'duplicates' ? '没有找到完全相同的文件' : kind === 'skipped' ? '没有跳过的文件' : '没有需要整理的文件'}</h3><p>{kind === 'items' ? '可以返回选择其他文件夹，或检查整理选项。' : '这是本次实际扫描的结果。'}</p></div> : kind === 'items' ? <div className="table-scroll"><table className="file-table"><thead><tr><th className="check-cell"><Checkbox checked={pageIds.length > 0 && pageSelected === pageIds.length} indeterminate={pageSelected > 0 && pageSelected < pageIds.length} label="选择本页文件" onChange={event => select(pageIds, event.target.checked)} /></th><th>文件</th><th>类型 / 大小</th><th>操作</th><th>整理后的位置</th></tr></thead><tbody>{(data.rows || []).map(row => <tr key={row.id} className={!isSelected(row.id) ? 'excluded-row' : ''}><td className="check-cell"><Checkbox checked={isSelected(row.id)} onChange={event => select([row.id], event.target.checked)} label={`选择 ${basename(row.source)}`} /></td><td><div className="filename"><File size={17} /><strong title={basename(row.source)}>{basename(row.source)}</strong>{(row.category === 'video' || mediaExtension.test(row.source || '')) && <MediaPreviewButton planId={plan.id} itemId={row.id} name={row.source} onOpen={setMediaRequest} />}</div><small className="file-path" title={row.source}>{row.source}</small></td><td><span>{categoryLabels[row.category] || row.category || '文件'}</span><small>{bytes(row.size)}</small></td><td><span className={`action-tag ${row.action === 'quarantine' ? 'warning' : ''}`}>{actionLabel(row.action)}</span></td><td><span className="destination-path" title={row.destination}>{row.destination || '原文件夹内的隔离区'}</span></td></tr>)}</tbody></table></div> : kind === 'duplicates' ? <div className="duplicate-groups">{(data.rows || []).map((group, index) => <details className="duplicate-group" key={group.id || index} open={index === 0}><summary><Files size={20} /><strong>{basename(group.keeper)}</strong><span>{number(group.files?.length)} 份相同内容 · 每份 {bytes(group.size)}</span></summary><div className="duplicate-files">{(group.files || []).map((file, i) => <div key={file.id || file.path || i} className={`duplicate-file ${file.keep ? 'keeper' : ''}`}><span className={`file-status ${file.keep ? 'safe' : ''}`}>{file.keep ? <ShieldCheck size={16} /> : <Files size={16} />}{file.keep ? '保留原件' : '重复副本'}</span><span className="duplicate-path" title={file.path}>{file.path}</span>{file.id && mediaExtension.test(file.path || '') && <MediaPreviewButton planId={plan.id} itemId={file.id} name={file.path} onOpen={setMediaRequest} compact />}{!file.keep && mode === 'dedup' && file.id && <Checkbox checked={isSelected(file.id)} label={`隔离 ${file.path}`} onChange={event => select([file.id], event.target.checked)} />}</div>)}</div></details>)}</div> : <div className="table-scroll"><table className="file-table skipped-table"><thead><tr><th>跳过的位置</th><th>原因</th></tr></thead><tbody>{(data.rows || []).map((row, index) => <tr key={row.id || index}><td><span className="skipped-path" title={row.path || row.source}>{row.path || row.source}</span></td><td>{row.reason || row.error || '受保护或不符合整理条件'}</td></tr>)}</tbody></table></div>}
      </div>
      <div className="pagination"><span>{data.total ? `${number(offset + 1)}–${number(Math.min(offset + 100, data.total))} / ${number(data.total)}` : '共 0 条'}<small>每页最多 100 条</small></span><div><button className="icon-button" aria-label="上一页" disabled={offset === 0 || loading} onClick={() => setOffset(value => Math.max(0, value - 100))}><ChevronLeft size={20} /></button><button className="icon-button" aria-label="下一页" disabled={offset + 100 >= (data.total || 0) || loading} onClick={() => setOffset(value => value + 100)}><ChevronRight size={20} /></button></div></div>
    </section>
    <div className="preview-footer"><button className="button secondary" onClick={onBack}><ArrowLeft size={17} />返回修改</button><div><span>将{actionLabel(operation)} <strong>{number(selectedCount)}</strong> 个文件</span><button className="button primary" disabled={selectedCount === 0 || plan.cancelled} onClick={() => { setConfirm(true); setAcknowledged(false); }}>确认整理<ArrowRight size={19} /></button></div></div>
    {confirm && <Modal title="确认本次整理" onClose={() => setConfirm(false)} busy={preparing} footer={<><button className="button secondary" disabled={preparing} onClick={() => setConfirm(false)}>再检查一下</button><button className="button primary" onClick={execute} disabled={preparing || (destructive && !acknowledged)}>{preparing && <LoaderCircle size={17} className="spin" />}确认{actionLabel(operation)} {number(selectedCount)} 个文件</button></>}>
      <div className="confirm-total"><ShieldCheck size={34} /><div><strong>{number(selectedCount)} 个文件</strong><span>{actionLabel(operation)} · {operation === 'copy' ? '保留原文件' : operation === 'quarantine' ? '保留至少一份原件' : '完整验证后移除原文件'}</span></div></div>
      {operation === 'copy' ? <p>文件会复制到预览中的位置，并验证内容完整性。原文件保留。同名文件会使用新名称，避免覆盖。</p> : operation === 'quarantine' ? <><p>仅处理内容完全相同的副本。副本将移动到各来源文件夹内的 <code>.fileatlas-quarantine</code> 隔离区，不会直接删除。</p><p>隔离期间仍占用磁盘空间，可以从“任务记录”恢复。</p></> : <p>文件完整复制并验证后，将移除原位置的文件。可以从“任务记录”尝试恢复；文件被其他程序修改或原位置被占用时会跳过并提示。</p>}
      {plan.config?.destination && <div className="confirm-destination"><Folder size={18} /><span>{plan.config.destination}</span></div>}
      <p className="field-help">执行前会再次核验文件。扫描后发生变化的文件会跳过，避免处理错误的内容。</p>
      {destructive && <label className="acknowledge"><input type="checkbox" checked={acknowledged} onChange={event => setAcknowledged(event.target.checked)} />我已查看预览，理解文件将被{operation === 'quarantine' ? '移入隔离区' : '移动到目标位置'}。</label>}
    </Modal>}
    {mediaRequest && <MediaPreview request={mediaRequest} onClose={() => setMediaRequest(null)} />}
  </div>;
}
