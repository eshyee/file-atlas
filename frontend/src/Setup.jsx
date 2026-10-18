import { useState } from 'react';
import { post } from './api.js';
import { Toggle, Notice } from './components.jsx';
import PrivilegeControl from './Privileges.jsx';
import { Folder, Files, FolderClosed, ChevronDown, ArrowRight, ShieldCheck, X, Merge, LoaderCircle } from './icons.js';

const categories = [{ id: 'picture', label: '照片与图片' }, { id: 'video', label: '视频' }, { id: 'audio', label: '音频' }, { id: 'document', label: '文档' }];
const modes = [
  { id: 'classify', name: '按类型分类', detail: '照片、视频、音频、文档，各归其位。', Icon: FolderClosed },
  { id: 'dedup', name: '查找重复文件', detail: '逐字节验证，只处理内容完全相同的文件。', Icon: Files },
];

export const initialConfig = mode => ({ sources: [], destination: '', mode, operation: 'copy', include_structured: false, categories: categories.map(c => c.id) });

export default function Setup({ config, setConfig, onScan, onModeChange, onError, busy, privileges, onElevate }) {
  const [path, setPath] = useState('');
  const [picking, setPicking] = useState(null);
  const [advanced, setAdvanced] = useState(false);
  const update = change => setConfig(current => ({ ...current, ...change }));
  const addPath = value => {
    const clean = value.trim().replace(/^"|"$/g, '');
    if (!clean) return;
    if (config.sources.some(source => source.toLowerCase() === clean.toLowerCase())) { onError('这个文件夹已经添加。'); return; }
    update({ sources: [...config.sources, clean] });
    setPath('');
  };
  const pick = async type => {
    setPicking(type);
    try {
      const result = await post('/api/pick-folder', {});
      if (result.path) type === 'source' ? addPath(result.path) : update({ destination: result.path });
    } catch (error) { onError(error.message); } finally { setPicking(null); }
  };
  const canScan = config.sources.length > 0 && (config.mode === 'dedup' || config.destination.trim()) && (config.mode !== 'classify' || config.categories.length > 0) && !busy && !picking;
  const scan = event => { event.preventDefault(); if (canScan) onScan(); };

  return <form onSubmit={scan} className="setup-form">
    <section className="panel source-panel" aria-labelledby="source-title">
      <div className="panel-heading"><h2 id="source-title">选择要整理的文件夹</h2><p>在开始前，先看看有哪些文件。</p></div>
      <div className="source-picker">
        <Folder className="source-folder" size={43} strokeWidth={1.7} aria-hidden="true" />
        <p>添加一个或多个文件夹</p>
        <button type="button" className="button primary picker-button" onClick={() => pick('source')} disabled={Boolean(picking)}>{picking === 'source' ? <LoaderCircle className="spin" size={17} /> : null}选择文件夹</button>
        {config.sources.length > 0 && <ul className="source-list" aria-label="已选择的文件夹">{config.sources.map(source => <li key={source}><Folder size={17} /><span title={source}>{source}</span><button className="icon-button" type="button" aria-label={`移除 ${source}`} onClick={() => update({ sources: config.sources.filter(s => s !== source) })}><X size={16} /></button></li>)}</ul>}
        <div className="path-entry"><label htmlFor="source-path">也可以直接输入文件夹路径</label><div className="input-button"><input id="source-path" value={path} onChange={event => setPath(event.target.value)} onKeyDown={event => { if (event.key === 'Enter') { event.preventDefault(); addPath(path); } }} placeholder="例如：D:\工作资料 或 C:\Users\你的名字\Documents" autoComplete="off" spellCheck="false" /><button className="button secondary" type="button" onClick={() => addPath(path)} disabled={!path.trim()}>添加</button></div></div>
      </div>
    </section>

    {config.mode !== 'merge' ? <div className="mode-options" role="radiogroup" aria-label="整理方式">{modes.map(({ id, name, detail, Icon }) => <button key={id} type="button" role="radio" aria-checked={config.mode === id} className={`mode-option ${config.mode === id ? 'selected' : ''}`} onClick={() => onModeChange(id)}><Icon size={30} strokeWidth={1.7} /><span><strong>{name}</strong><small>{detail}</small></span><span className="radio-dot" /></button>)}</div> : <div className="merge-explanation"><Merge size={26} /><div><strong>把多个位置的文件，汇入一个文件夹</strong><p>保留相对目录，自动避开同名冲突；默认复制，源文件保留。</p></div></div>}

    <section className="panel options-panel" aria-labelledby="options-title"><h2 id="options-title">整理选项</h2>
      {config.mode !== 'dedup' ? <div className="destination-field"><label htmlFor="destination">{config.mode === 'merge' ? '选择归并后的保存位置' : '选择分类后的保存位置'}</label><div className="input-button"><input id="destination" value={config.destination} onChange={event => update({ destination: event.target.value })} placeholder="例如：D:\归序整理后的文件" autoComplete="off" spellCheck="false" /><button type="button" className="button secondary" onClick={() => pick('destination')} disabled={Boolean(picking)}>{picking === 'destination' ? <LoaderCircle size={16} className="spin" /> : '浏览'}</button></div></div> : <p className="dedup-note"><ShieldCheck size={18} />重复副本会移入原文件夹内的隔离区，至少保留一份原件，可从任务记录恢复。</p>}
      <div className="toggle-row">{config.mode !== 'dedup' && <Toggle checked={config.operation === 'copy'} onChange={event => update({ operation: event.target.checked ? 'copy' : 'move' })} hint="复制完成并验证内容后，原文件仍保留。">默认复制，保留原文件</Toggle>}<Toggle checked locked hint="始终跳过系统、程序、开发工程、链接及归序隔离区。">保护工程与系统目录</Toggle></div>
      {config.operation === 'move' && config.mode !== 'dedup' && <><Notice type="warning"><strong>已选择移动</strong><p>验证目标文件完整后才移除原文件。确认整理时，需要再次同意此操作。</p></Notice>{privileges && !privileges.elevated && <div className="move-permission"><div><strong>当前是标准权限</strong><p>普通文件可以直接移动；如果 Windows 拒绝访问，请先以管理员权限重新启动，再重新扫描。</p></div><PrivilegeControl status={privileges} onElevate={onElevate} compact /></div>}</>}
      <button type="button" className="disclosure" aria-expanded={advanced} onClick={() => setAdvanced(!advanced)}><ChevronDown size={17} className={advanced ? 'rotated' : ''} />更多选项</button>
      {advanced && <div className="advanced-options">{config.mode === 'classify' ? <><fieldset><legend>要分类的文件类型</legend><div className="category-options">{categories.map(category => <label key={category.id}><input type="checkbox" checked={config.categories.includes(category.id)} onChange={event => update({ categories: event.target.checked ? [...config.categories, category.id] : config.categories.filter(value => value !== category.id) })} />{category.label}</label>)}</div><p className="field-help">未勾选的类型和不支持的格式会列入跳过清单。</p></fieldset><label className="check-label"><input type="checkbox" checked={config.include_structured} onChange={event => update({ include_structured: event.target.checked })} />将 JSON、YAML 等配置文件也归入文档</label><p className="field-help">默认保留这类配置文件，避免拆散配套资料；工程目录始终受保护。</p></> : <p className="field-help">系统目录、工程目录、符号链接、快捷链接与归序隔离区始终受保护。可以在扫描后查看具体跳过原因。</p>}</div>}
    </section>

    <div className="setup-footer"><div className="safety-banner"><ShieldCheck size={35} strokeWidth={1.6} /><div><strong>安全从预览开始</strong><p>扫描不会修改文件；所有操作先确认，重复文件可恢复。</p></div></div><button className="button primary scan-button" type="submit" disabled={!canScan}>开始扫描<ArrowRight size={22} /></button></div>
  </form>;
}
