import { useEffect, useState } from 'react';
import { basename, post } from './api.js';
import { Modal, Notice } from './components.jsx';
import { Film, Image, LoaderCircle, Play } from './icons.js';

export function MediaPreviewButton({ planId, itemId, name, onOpen, compact = false }) {
  return <button type="button" className={`media-preview-button ${compact ? 'compact' : ''}`} onClick={() => onOpen({ planId, itemId, name })} aria-label={`预览 ${basename(name)}`}>{compact ? <Play size={14} /> : <><Play size={14} />预览</>}</button>;
}

export default function MediaPreview({ request, onClose }) {
  const [media, setMedia] = useState(null);
  const [error, setError] = useState('');
  const [converting, setConverting] = useState(false);
  const prepare = async convert => {
    setConverting(Boolean(convert));
    setError('');
    try { setMedia(await post('/api/preview', { plan_id: request.planId, item_id: request.itemId, convert })); }
    catch (reason) { if (reason.name !== 'AbortError') setError(reason.message); }
    finally { setConverting(false); }
  };
  useEffect(() => {
    const controller = new AbortController();
    post('/api/preview', { plan_id: request.planId, item_id: request.itemId }, { signal: controller.signal })
      .then(setMedia)
      .catch(reason => { if (reason.name !== 'AbortError') setError(reason.message); });
    return () => controller.abort();
  }, [request]);
  return <Modal title={basename(request.name)} onClose={onClose} footer={<button className="button primary" onClick={onClose}>关闭预览</button>}>
    <div className="media-viewer">
      {!media && !error && <div className="media-loading"><LoaderCircle className="spin" size={31} /><span>正在准备本机预览…</span></div>}
      {error && <Notice type="warning" title="无法直接预览这个文件"><p>{error}</p>{media?.can_convert && !media.converted ? <button type="button" className="button secondary media-convert" onClick={() => prepare(true)} disabled={converting}>{converting && <LoaderCircle className="spin" size={16} />}生成兼容预览</button> : <p>原文件没有被修改，可以使用系统中的播放器或图片工具打开。</p>}</Notice>}
      {media?.kind === 'image' && !error && <div className="image-viewer"><img src={media.url} alt={basename(request.name)} onError={() => setError('图片格式无法由当前浏览器解码。')} /></div>}
      {media?.kind === 'video' && !error && <div className="video-viewer"><video src={media.url} poster={media.poster_url || undefined} controls preload="metadata" playsInline onError={() => setError('视频格式无法由当前浏览器解码。')}>当前浏览器无法播放这个视频。</video></div>}
      {media && !error && <div className="media-caption"><span>{media.kind === 'video' ? <Film size={17} /> : <Image size={17} />}{media.converted ? '已生成临时兼容预览' : '直接读取原文件'}</span><small>{media.limited ? `兼容预览只包含前 ${media.duration_limit || 30} 秒；原视频保持完整。` : media.converted ? '临时文件位于归序状态目录，不会改写原件。' : '只读预览，不会修改文件。'}</small></div>}
    </div>
  </Modal>;
}
