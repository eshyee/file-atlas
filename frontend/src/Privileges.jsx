import { useState } from 'react';
import { createPortal } from 'react-dom';
import { Modal, Notice } from './components.jsx';
import { CheckCircle2, KeyRound, LoaderCircle, Shield, ShieldCheck } from './icons.js';

export default function PrivilegeControl({ status, onElevate, compact = false }) {
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [outcome, setOutcome] = useState(null);
  const [error, setError] = useState('');
  if (!status) return null;
  const elevated = status.elevated === true;
  const windows = status.platform === 'windows';
  const canRequest = status.can_request === true;
  const unknown = status.reason === 'detection_failed';
  const label = unknown ? '权限未确认' : elevated ? (windows ? '管理员权限' : '系统高权限') : '标准权限';
  const openDetails = () => { setOpen(true); setOutcome(null); setError(''); };
  const elevate = async () => {
    setBusy(true);
    setError('');
    setOutcome(null);
    try {
      const result = await onElevate();
      if (result.cancelled) setOutcome('cancelled');
      else if (result.already_elevated) setOutcome('already_elevated');
      else if (result.launched) setOutcome('launched');
      else throw new Error(result.message || 'Windows 未确认新的管理员会话启动请求。');
    }
    catch (reason) { setError(reason.message || '无法确认管理员会话的启动请求，请检查是否出现新的归序窗口。'); }
    finally { setBusy(false); }
  };
  return <>
    <button type="button" aria-label={`文件访问权限：${label}`} title={status.display} className={`privilege-trigger ${compact ? 'compact' : ''} ${elevated ? 'elevated' : ''}`} onClick={openDetails}><span>{elevated ? <ShieldCheck size={18} /> : <Shield size={18} />}</span><span><strong>{label}</strong>{!compact && <small>{unknown ? '查看权限检测说明' : elevated ? '仍遵循文件保护与访问规则' : canRequest ? '可另开管理员会话' : '遵循当前系统权限'}</small>}</span></button>
    {open && createPortal(<Modal title="文件访问权限" onClose={() => setOpen(false)} busy={busy} footer={<>{!elevated && canRequest && outcome !== 'launched' && <button className="button primary" onClick={elevate} disabled={busy}>{busy ? <LoaderCircle className="spin" size={17} /> : <KeyRound size={17} />}{outcome === 'cancelled' ? '再次请求管理员会话' : '打开新的管理员会话'}</button>}<button className="button secondary" onClick={() => setOpen(false)} disabled={busy}>返回当前会话</button></>}>
      <div className="privilege-explanation"><Shield size={37} /><div><strong>当前窗口：{label}</strong><p>{unknown ? status.display || '无法确认当前进程权限，请通过系统菜单检查后重新打开归序。' : elevated ? '当前会话具有较高权限，仍需遵循 Windows 访问规则与归序的文件保护。是否可以处理某个文件，以扫描和执行检查结果为准。' : '当前窗口保持标准权限。扫描、预览和普通文件夹中的复制通常可直接进行。'}</p></div></div>
      {outcome === 'launched' ? <Notice title="Windows 已接受新会话启动请求"><p>请切换到新打开的归序窗口，检查左下角是否显示“管理员权限”，再重新选择文件夹、扫描并确认。</p><p>当前窗口保留{label}，仍可使用；当前任务不会自动重放。</p></Notice> : outcome === 'cancelled' ? <Notice title="已取消管理员权限请求"><p>本次未启动新的管理员会话。当前窗口和任务保持原样，可以继续使用当前权限，也可再次主动请求。</p></Notice> : outcome === 'already_elevated' || elevated ? <Notice title="当前会话已具有较高权限"><p>无需重复请求。文件占用、访问限制和应用保护仍然适用；任务仍需先预览再确认。</p></Notice> : unknown ? <Notice type="warning" title="暂时无法确认权限状态"><p>本次不会提供 UAC 请求按钮。请检查系统权限策略，或使用系统“以管理员身份运行”入口重新打开归序。</p></Notice> : canRequest ? <Notice type="warning"><p>新的管理员会话需要通过 Windows UAC 确认。当前窗口的权限不会改变，新窗口需要重新扫描、预览和确认。</p></Notice> : <Notice><p>{status.display || '当前系统未提供 Windows UAC 请求入口，归序会遵循启动它的帐户权限。'}</p></Notice>}
      {error && <Notice type="error" title="未能确认新的管理员会话请求"><p>{error}</p><p>请检查是否出现 UAC 提示或新的归序窗口。当前会话不会改变权限，也不会自动执行文件操作。</p></Notice>}
      <div className="privilege-rules"><p><CheckCircle2 size={17} />不会把当前浏览器会话密钥传给管理员进程</p><p><CheckCircle2 size={17} />不会因为提权而自动执行或重放文件操作</p><p><CheckCircle2 size={17} />系统目录和工程目录保护仍然开启</p></div>
    </Modal>, document.body)}
  </>;
}
