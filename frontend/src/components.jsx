import { useEffect, useRef } from 'react';
import { AlertTriangle, Check, CircleHelp, LockKeyhole, ShieldCheck, X } from './icons.js';

export function Notice({ children, type = 'safe', title, className = '' }) {
  const Icon = type === 'safe' ? ShieldCheck : AlertTriangle;
  return <div className={`notice notice-${type} ${className}`} role={type === 'error' ? 'alert' : undefined}>
    <Icon size={20} aria-hidden="true" /><div>{title && <strong>{title}</strong>}{children}</div>
  </div>;
}

export function Toggle({ checked, onChange, children, locked = false, hint }) {
  return <label className={`toggle-field ${locked ? 'is-locked' : ''}`} title={hint}>
    <input type="checkbox" role="switch" checked={checked} onChange={onChange} disabled={locked} />
    <span className="toggle-track" aria-hidden="true"><span /></span>
    <span>{children}</span>
    {locked ? <LockKeyhole size={15} aria-label="始终开启" /> : hint && <CircleHelp className="muted" size={14} aria-hidden="true" />}
  </label>;
}

export function Steps({ step = 1 }) {
  return <ol className="steps" aria-label="整理步骤">
    {['选择文件夹', '扫描与预览', '确认整理'].map((label, i) => <li className={step >= i + 1 ? 'active' : ''} key={label} aria-current={step === i + 1 ? 'step' : undefined}>
      <span className="step-number">{step > i + 1 ? <Check size={17} /> : i + 1}</span><span>{label}</span>
    </li>)}
  </ol>;
}

export function Checkbox({ checked, indeterminate, onChange, label, ...props }) {
  const ref = useRef(null);
  useEffect(() => { if (ref.current) ref.current.indeterminate = Boolean(indeterminate); }, [indeterminate]);
  return <input ref={ref} className="checkbox" type="checkbox" checked={checked} onChange={onChange} aria-label={label} {...props} />;
}

export function Modal({ title, children, onClose, footer, busy = false }) {
  const ref = useRef(null);
  const closeRef = useRef(onClose);
  closeRef.current = onClose;
  useEffect(() => {
    const previous = document.activeElement;
    const node = ref.current;
    const elements = () => Array.from(node.querySelectorAll('button:not(:disabled), input:not(:disabled), [tabindex="0"]'));
    elements()[0]?.focus();
    const handleKey = event => {
      if (event.key === 'Escape' && !busy) closeRef.current();
      if (event.key !== 'Tab') return;
      const all = elements();
      const first = all[0], last = all[all.length - 1];
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus(); }
      if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus(); }
    };
    document.addEventListener('keydown', handleKey);
    return () => { document.removeEventListener('keydown', handleKey); previous?.focus(); };
  }, [busy]);
  return <div className="modal-backdrop"><section ref={ref} className="modal" role="dialog" aria-modal="true" aria-labelledby="dialog-title">
    <div className="modal-header"><h2 id="dialog-title">{title}</h2><button className="icon-button" onClick={onClose} disabled={busy} aria-label="关闭"><X size={20} /></button></div>
    <div className="modal-content">{children}</div><div className="modal-footer">{footer}</div>
  </section></div>;
}
