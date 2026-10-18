import { useCallback, useEffect, useMemo, useState } from 'react';
import { api, basename, number, post } from './api.js';
import { Modal, Notice } from './components.jsx';
import {
  ArrowLeft, ArrowRight, ChevronLeft, ChevronRight, Folder, Image,
  Images, LoaderCircle, ScanFace, ShieldCheck, UserRound, UsersRound, X,
} from './icons.js';

const PAGE_SIZE = 24;
const personLabel = id => `人物 ${String(id || "").slice(0, 8).toUpperCase()}`;

function FaceThumb({ imageId, faceId, alt = '', size = 480, className = '' }) {
  const [url, setUrl] = useState('');
  const [failed, setFailed] = useState(false);
  useEffect(() => {
    if (!imageId) return undefined;
    const controller = new AbortController();
    let objectUrl = '';
    const params = new URLSearchParams({ image_id: String(imageId), max_size: String(size) });
    if (faceId) params.set('face_id', String(faceId));
    api(`/api/faces/thumbnail?${params}`, { blob: true, signal: controller.signal })
      .then(blob => { objectUrl = URL.createObjectURL(blob); setUrl(objectUrl); setFailed(false); })
      .catch(error => { if (error.name !== 'AbortError') setFailed(true); });
    return () => { controller.abort(); if (objectUrl) URL.revokeObjectURL(objectUrl); };
  }, [imageId, faceId, size]);
  if (!url || failed) return <span className={`face-thumb-placeholder ${className}`} aria-label={failed ? '预览不可用' : '正在加载预览'}><Image size={28} /></span>;
  return <img className={className} src={url} alt={alt} loading="lazy" decoding="async" />;
}

function Pager({ offset, total, onChange }) {
  if (total <= PAGE_SIZE) return null;
  return <div className="face-pager"><span>{number(offset + 1)}–{number(Math.min(offset + PAGE_SIZE, total))} / {number(total)}</span><div><button className="icon-button" aria-label="上一页" disabled={offset === 0} onClick={() => onChange(Math.max(0, offset - PAGE_SIZE))}><ChevronLeft size={20} /></button><button className="icon-button" aria-label="下一页" disabled={offset + PAGE_SIZE >= total} onClick={() => onChange(offset + PAGE_SIZE)}><ChevronRight size={20} /></button></div></div>;
}

export default function Faces({ onBusyChange, onError }) {
  const [capability, setCapability] = useState(null);
  const [sources, setSources] = useState([]);
  const [path, setPath] = useState('');
  const [modelDir, setModelDir] = useState('');
  const [similarity, setSimilarity] = useState(0.55);
  const [picking, setPicking] = useState('');
  const [job, setJob] = useState(null);
  const [people, setPeople] = useState({ rows: [], total: 0 });
  const [peopleOffset, setPeopleOffset] = useState(0);
  const [person, setPerson] = useState(null);
  const [photos, setPhotos] = useState({ rows: [], total: 0 });
  const [photosOffset, setPhotosOffset] = useState(0);
  const [viewer, setViewer] = useState(null);
  const [loading, setLoading] = useState(true);

  const busy = Boolean(job && ['running', 'cancelling'].includes(job.status));
  useEffect(() => { onBusyChange?.(busy); return () => onBusyChange?.(false); }, [busy, onBusyChange]);

  const readCapability = useCallback(async () => {
    const result = await api('/api/faces/capabilities');
    setCapability(result);
    if (!modelDir && result.model_dir) setModelDir(result.model_dir);
    return result;
  }, [modelDir]);
  const readPeople = useCallback(async (offset = peopleOffset) => {
    const params = new URLSearchParams({ offset: String(offset), limit: String(PAGE_SIZE) });
    const result = await api(`/api/faces/people?${params}`);
    setPeople(result);
  }, [peopleOffset]);
  const readPhotos = useCallback(async (selected, offset = photosOffset) => {
    if (!selected) return;
    const params = new URLSearchParams({ person_id: selected.person_id, offset: String(offset), limit: String(PAGE_SIZE) });
    setPhotos(await api(`/api/faces/photos?${params}`));
  }, [photosOffset]);

  useEffect(() => {
    const controller = new AbortController();
    Promise.all([
      api('/api/faces/capabilities', { signal: controller.signal }),
      api(`/api/faces/people?${new URLSearchParams({ offset: '0', limit: String(PAGE_SIZE) })}`, { signal: controller.signal }),
    ]).then(([caps, groups]) => {
      setCapability(caps); setModelDir(caps.model_dir || ''); setPeople(groups);
    }).catch(error => { if (error.name !== 'AbortError') onError(error.message); }).finally(() => setLoading(false));
    return () => controller.abort();
  }, [onError]);

  useEffect(() => {
    if (!job?.id || !busy) return undefined;
    const controller = new AbortController();
    let timer;
    const poll = async () => {
      try {
        const next = await api(`/api/job?${new URLSearchParams({ id: job.id })}`, { signal: controller.signal });
        setJob(next);
        if (['running', 'cancelling'].includes(next.status)) timer = setTimeout(poll, 650);
        else if (next.status === 'completed') {
          setPeopleOffset(0); setPerson(null); setPhotosOffset(0);
          await Promise.all([readCapability(), readPeople(0)]);
        }
      } catch (error) {
        if (error.name !== 'AbortError') { onError(error.message); timer = setTimeout(poll, 1600); }
      }
    };
    poll();
    return () => { controller.abort(); clearTimeout(timer); };
  }, [job?.id, busy, onError, readCapability, readPeople]);

  const addPath = value => {
    const clean = value.trim().replace(/^"|"$/g, '');
    if (!clean) return;
    if (sources.some(item => item.toLowerCase() === clean.toLowerCase())) { onError('这个图片文件夹已经添加。'); return; }
    setSources(current => [...current, clean]); setPath('');
  };
  const pick = async type => {
    setPicking(type);
    try {
      const result = await post('/api/pick-folder', {});
      if (!result.path) return;
      if (type === 'source') addPath(result.path);
      else {
        setModelDir(result.path);
        setCapability(await post('/api/faces/capabilities', { model_dir: result.path }));
      }
    } catch (error) { onError(error.message); } finally { setPicking(''); }
  };
  const build = async event => {
    event.preventDefault();
    try {
      const result = await post('/api/faces/build', { sources, model_dir: modelDir || undefined, similarity });
      setJob({ id: result.job_id, action: 'faces', status: 'running', progress: {} });
    } catch (error) { onError(error.message); }
  };
  const cancel = async () => {
    try { await post('/api/cancel', { job_id: job.id }); setJob(current => ({ ...current, status: 'cancelling' })); }
    catch (error) { onError(error.message); }
  };
  const openPerson = async group => {
    setPerson(group); setPhotosOffset(0); setLoading(true);
    try { await readPhotos(group, 0); } catch (error) { onError(error.message); } finally { setLoading(false); }
  };
  const changePeoplePage = async offset => {
    setPeopleOffset(offset); setLoading(true);
    try { await readPeople(offset); } catch (error) { onError(error.message); } finally { setLoading(false); }
  };
  const changePhotosPage = async offset => {
    setPhotosOffset(offset); setLoading(true);
    try { await readPhotos(person, offset); } catch (error) { onError(error.message); } finally { setLoading(false); }
  };

  const ready = Boolean(capability?.ready ?? capability?.available);
  const hasIndex = Boolean(capability?.has_index ?? capability?.index_exists ?? people.total);
  const progress = job?.progress || {};
  const progressRatio = progress.total > 0 ? Math.min(100, Math.round((Number(progress.current || 0) / Number(progress.total)) * 100)) : null;
  const stats = useMemo(() => capability?.stats || {}, [capability]);

  if (loading && capability === null) return <div className="face-loading"><LoaderCircle className="spin" size={30} /><span>正在读取本机人脸索引…</span></div>;

  if (person) return <div className="faces-view"><div className="face-browser-heading"><button className="button secondary" onClick={() => { setPerson(null); setViewer(null); }}><ArrowLeft size={17} />返回人物索引</button><div><h2 title={person.person_id}>{personLabel(person.person_id)}</h2><p>{number(photos.total)} 张照片 · 编号只代表相似人脸组，不是现实身份</p></div></div>
    {loading ? <div className="face-loading"><LoaderCircle className="spin" size={28} />正在读取照片…</div> : <div className="photo-grid">{photos.rows.map(photo => <button className="photo-card" key={`${photo.image_id}-${photo.face_id || ''}`} onClick={() => setViewer(photo)}><span className="photo-frame"><FaceThumb imageId={photo.image_id} alt={basename(photo.path || photo.relative_path)} size={620} className="photo-image" />{photo.face_count > 1 && <span className="photo-face-count"><UsersRound size={13} />{photo.face_count}</span>}</span><strong title={photo.path || photo.relative_path}>{basename(photo.path || photo.relative_path)}</strong><small>{photo.relative_path || '本地照片'}</small></button>)}</div>}
    {!loading && photos.rows.length === 0 && <div className="empty-state"><Images size={42} /><h3>这个索引组暂时没有可浏览照片</h3><p>图片可能已移走；重新建立索引即可更新。</p></div>}
    <Pager offset={photosOffset} total={photos.total || 0} onChange={changePhotosPage} />
    {viewer && <Modal title={basename(viewer.path || viewer.relative_path)} onClose={() => setViewer(null)} footer={<button className="button primary" onClick={() => setViewer(null)}>关闭预览</button>}><div className="face-photo-viewer"><FaceThumb imageId={viewer.image_id} alt={basename(viewer.path || viewer.relative_path)} size={1024} className="face-photo-full" /><p title={viewer.path}>{viewer.relative_path || viewer.path}</p></div></Modal>}
  </div>;

  return <div className="faces-view">
    <section className="face-privacy"><div><ScanFace size={31} /><span><strong>人脸特征只保存在这台电脑</strong><small>原图只读，不上传、不移动、不删除，也不推测姓名、年龄或性别。</small></span></div><ShieldCheck size={25} /></section>
    {!ready && <Notice type="warning" title="人脸引擎尚未就绪"><p>{capability?.message || '需要本地 OpenCV 人脸引擎，以及 YuNet 与 SFace 模型文件。'}</p>{capability?.missing?.length > 0 && <p>缺少：{capability.missing.join('、')}</p>}</Notice>}
    <form className="panel face-setup" onSubmit={build}>
      <div className="face-setup-title"><div><h2>建立本机人脸索引</h2><p>首次扫描可能需要较长时间；以后仍会核验照片内容，只复用完全未变化的结果。</p></div>{hasIndex && <span className="index-status"><span />已有索引</span>}</div>
      <div className="face-source-layout"><div className="face-source-main"><label>图片文件夹</label><div className="input-button"><input value={path} onChange={event => setPath(event.target.value)} placeholder="例如：D:\照片" autoComplete="off" spellCheck="false" onKeyDown={event => { if (event.key === 'Enter') { event.preventDefault(); addPath(path); } }} /><button type="button" className="button primary" onClick={() => pick('source')} disabled={Boolean(picking)}>{picking === 'source' && <LoaderCircle className="spin" size={16} />}选择文件夹</button></div>
        {sources.length > 0 && <ul className="source-list face-source-list">{sources.map(source => <li key={source}><Folder size={17} /><span title={source}>{source}</span><button type="button" className="icon-button" aria-label={`移除 ${source}`} onClick={() => setSources(current => current.filter(item => item !== source))}><X size={16} /></button></li>)}</ul>}</div>
        <div className="face-controls"><label htmlFor="face-models">模型文件夹</label><div className="input-button"><input id="face-models" value={modelDir} onChange={event => setModelDir(event.target.value)} onBlur={async () => { if (modelDir) { try { setCapability(await post('/api/faces/capabilities', { model_dir: modelDir })); } catch (error) { onError(error.message); } } }} placeholder="完整版已内置，无需选择" autoComplete="off" spellCheck="false" /><button type="button" className="button secondary" onClick={() => pick('model')} disabled={Boolean(picking)}>{picking === 'model' ? <LoaderCircle className="spin" size={16} /> : '浏览'}</button></div><label htmlFor="face-similarity">分组严格度 <strong>{similarity.toFixed(2)}</strong></label><input id="face-similarity" type="range" min="0.45" max="0.70" step="0.01" value={similarity} onChange={event => setSimilarity(Number(event.target.value))} /><small>数值越高，误合并更少，但同一人可能分成多个组。建议先用 0.55。</small></div></div>
      {busy && <div className="face-job"><div className={`progress-track ${progressRatio === null ? 'indeterminate' : ''}`} role="progressbar" aria-valuemin={0} aria-valuemax={100} aria-valuenow={progressRatio ?? undefined}><span style={progressRatio === null ? undefined : { width: `${progressRatio}%` }} /></div><div><span>{progress.message || (job.status === 'cancelling' ? '正在安全停止…' : '正在读取照片并构建索引…')}</span><strong>{progress.total > 0 ? `${number(progress.current)} / ${number(progress.total)}` : '准备中'}</strong></div></div>}
      {job && !busy && <Notice type={job.status === 'completed' ? 'safe' : 'warning'} title={job.status === 'completed' ? '索引已更新' : job.status === 'cancelled' ? '已停止建立索引' : '这次索引未完成'}><p>{job.status === 'completed' ? `已识别 ${number(job.result?.faces || job.result?.face_count)} 张人脸，归入 ${number(job.result?.people || job.result?.person_count)} 个索引组。` : job.error || '上一版完整索引仍可继续浏览。'}</p></Notice>}
      <div className="face-setup-footer"><p><strong>{number(stats.indexed ?? stats.images ?? stats.photo_count ?? 0)}</strong> 张照片 · <strong>{number(stats.faces || stats.face_count || 0)}</strong> 张人脸 · <strong>{number(stats.people || stats.person_count || people.total || 0)}</strong> 个索引组</p>{busy ? <button type="button" className="button secondary" onClick={cancel} disabled={job.status === 'cancelling'}>{job.status === 'cancelling' ? <LoaderCircle className="spin" size={17} /> : null}{job.status === 'cancelling' ? '正在停止' : '停止索引'}</button> : <button type="submit" className="button primary" disabled={!ready || sources.length === 0}>建立 / 更新索引<ArrowRight size={18} /></button>}</div>
    </form>

    <div className="face-browser-heading"><div><h2>按相似人脸浏览</h2><p>每组都可以打开查看照片；可能存在误分组，请把它当作整理线索。</p></div><span><UserRound size={17} />{number(people.total || 0)} 组</span></div>
    {people.rows.length > 0 ? <div className="people-grid">{people.rows.map(group => <button className="person-card" key={group.person_id} onClick={() => openPerson(group)}><span className="person-photo"><FaceThumb imageId={group.representative_image_id || group.image_id || group.representative?.image_id} faceId={group.representative_face_id || group.face_id || group.representative?.face_id} alt={`${personLabel(group.person_id)} 代表人脸`} size={420} className="person-image" /></span><span className="person-info"><strong title={group.person_id}>{personLabel(group.person_id)}</strong><small>{number(group.photo_count)} 张照片 · {number(group.face_count)} 张人脸</small></span><ChevronRight size={19} /></button>)}</div> : <div className="empty-state face-empty"><ScanFace size={44} /><h3>还没有人脸索引</h3><p>选择图片文件夹后建立索引，识别出的相似人脸会在这里分组显示。</p></div>}
    <Pager offset={peopleOffset} total={people.total || 0} onChange={changePeoplePage} />
  </div>;
}
