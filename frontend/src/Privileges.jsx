import { useState } from 'react';
import { Modal, Notice } from './components.jsx';
import { CheckCircle2, KeyRound, LoaderCircle, Shield, ShieldCheck } from './icons.js';

export default function PrivilegeControl({ status, onElevate, compact = false }) {
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [requested, setRequested] = useState(false);
  const [error, setError] = useState('');
  if (!status) return null;
  const elevated = Boolean(status.elevated);
  const windows = status.platform === 'windows' || status.can_request;
  const elevate = async () => {
    setBusy(true);
    setError('');
    try { await onElevate(); setRequested(true); }
    catch (reason) { setError(reason.message || '管理员会话未能启动。'); }
    finally { setBusy(false); }
  };
  return <>
    <button type="button" className={`privilege-trigger ${compact ? 'compact' : ''} ${elevated ? 'elevated' : ''}`} onClick={() => setOpen(true)}><span>{elevated ? <ShieldCheck size={18} /> : <Shield size={18} />}</span><span><strong>{elevated ? '管理员权限' : '标准权限'}</strong>{!compact && <small>{elevated ? '可访问已授权的受保护文件' : windows ? '需要时可通过 UAC 提升' : '遵循当前系统权限'}</small>}</span></button>
    {open && <Modal title="文件访问权限" onClose={() => setOpen(false)} busy={busy} footer={<>{!elevated && windows && !requested && <button className="button primary" onClick={elevate} disabled={busy}>{busy ? <LoaderCircle className="spin" size={17} /> : <KeyRound size={17} />}以管理员权限重新启动</button>}<button className="button secondary" onClick={() => setOpen(false)} disabled={busy}>{requested ? '关闭当前页面' : '保持当前权限'}</button></>}>
      {elevated ? <Notice title="归序已使用管理员权限"><p>可以读取或移动当前 Windows 帐户获准管理的受保护文件。整理前仍需扫描、预览和确认。</p></Notice> : requested ? <Notice title="已向 Windows 请求管理员权限"><p>请在 UAC 窗口中确认。新的归序会话会单独打开；当前任务不会自动重放。</p></Notice> : <><div className="privilege-explanation"><Shield size={37} /><div><strong>当前使用标准权限</strong><p>扫描、预览和普通文件夹中的复制通常不需要管理员权限。遇到受保护来源时，移动可能会被 Windows 拒绝。</p></div></div>{windows ? <Notice type="warning"><p>提升会打开一个新的管理员会话。为防止旧预览在权限变化后直接执行，你需要重新选择文件夹、扫描并确认。</p></Notice> : <Notice><p>当前系统不使用 Windows UAC。归序会遵循启动它的帐户权限。</p></Notice>}</>}
      {error && <Notice type="error" title="管理员会话未启动"><p>{error}</p></Notice>}
      <div className="privilege-rules"><p><CheckCircle2 size={17} />不会把当前浏览器会话密钥传给管理员进程</p><p><CheckCircle2 size={17} />不会因为提权而自动执行或重放文件操作</p><p><CheckCircle2 size={17} />系统目录和工程目录保护仍然开启</p></div>
    </Modal>}
  </>;
}
