import { useEffect, useRef, type ReactNode } from 'react';
import { createPortal } from 'react-dom';
import styles from './Menu.module.css';

export interface MenuItem {
 label: string;
 icon?: ReactNode;
 onClick: () => void;
 danger?: boolean;
}

interface MenuProps {
 open: boolean;
 x: number;
 y: number;
 items: MenuItem[];
 onClose: () => void;
}

export function Menu({ open, x, y, items, onClose }: MenuProps) {
 const ref = useRef<HTMLDivElement>(null);

 useEffect(() => {
   if (!open) return;
   const onDown = (e: MouseEvent) => {
     if (ref.current && !ref.current.contains(e.target as Node)) onClose();
   };
   const onKey = (e: KeyboardEvent) => e.key === 'Escape' && onClose();
   window.addEventListener('mousedown', onDown);
   window.addEventListener('keydown', onKey);
   return () => {
     window.removeEventListener('mousedown', onDown);
     window.removeEventListener('keydown', onKey);
   };
 }, [open, onClose]);

 if (!open) return null;

 return createPortal(
   <div ref={ref} className={styles.menu} style={{ top: y, left: x }} role="menu">
     {items.map((it, i) => (
       <button
         key={i}
         type="button"
         role="menuitem"
         className={[styles.item, it.danger ? styles.danger : ''].filter(Boolean).join(' ')}
         onClick={() => {
           it.onClick();
           onClose();
         }}
       >
         {it.icon && <span className={styles.icon}>{it.icon}</span>}
         {it.label}
       </button>
     ))}
   </div>,
   document.body,
 );
}