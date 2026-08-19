import type { ReactNode } from 'react';
import { Modal } from '../Modal';
import { Button } from '../Button';
import styles from './ConfirmDialog.module.css';

interface ConfirmDialogProps {
 open: boolean;
 title: string;
 message?: ReactNode;
 confirmLabel?: string;
 cancelLabel?: string;
 danger?: boolean;
 onConfirm: () => void;
 onCancel: () => void;
}

export function ConfirmDialog({
 open,
 title,
 message,
 confirmLabel = 'Xác nhận',
 cancelLabel = 'Huỷ',
 danger = false,
 onConfirm,
 onCancel,
}: ConfirmDialogProps) {
 return (
   <Modal
     open={open}
     onClose={onCancel}
     title={title}
     width={400}
     footer={
       <>
         <Button onClick={onCancel}>{cancelLabel}</Button>
         <Button variant={danger ? 'danger' : 'primary'} onClick={onConfirm}>
           {confirmLabel}
         </Button>
       </>
     }
   >
     {message && <p className={styles.message}>{message}</p>}
   </Modal>
 );
}