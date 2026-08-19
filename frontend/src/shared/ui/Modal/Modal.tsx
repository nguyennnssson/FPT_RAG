import { useEffect, type ReactNode } from 'react';
import { createPortal } from 'react-dom';
import { X } from 'lucide-react';
import { IconButton } from '../IconButton';
import styles from './Modal.module.css';

interface ModalProps {
 open: boolean;
 onClose: () => void;
 title?: ReactNode;
 children: ReactNode;
 footer?: ReactNode;
 width?: number;
}

export function Modal({ open, onClose, title, children, footer, width = 440 }: ModalProps) {
 useEffect(() => {
   if (!open) return;
   const onKey = (e: KeyboardEvent) => e.key === 'Escape' && onClose();
   window.addEventListener('keydown', onKey);
   return () => window.removeEventListener('keydown', onKey);
 }, [open, onClose]);

 if (!open) return null;

 return createPortal(
   <div className={styles.overlay} onMouseDown={onClose}>
     <div
       className={styles.dialog}
       style={{ width }}
       role="dialog"
       aria-modal="true"
       onMouseDown={(e) => e.stopPropagation()}
     >
       {title && (
         <div className={styles.header}>
           <div className={styles.title}>{title}</div>
           <IconButton label="Đóng" onClick={onClose}>
             <X size={18} />
           </IconButton>
         </div>
       )}
       <div className={styles.body}>{children}</div>
       {footer && <div className={styles.footer}>{footer}</div>}
     </div>
   </div>,
   document.body,
 );
}