import { Check, Plus } from 'lucide-react';
import { Modal } from '../../../../shared/ui/Modal';
import { Avatar } from '../../../../shared/ui/Avatar';
import styles from './SwitchAccountDialog.module.css';

export interface Account {
 id: string;
 name: string;
 initials: string;
 email?: string;
}

interface SwitchAccountDialogProps {
 open: boolean;
 accounts: Account[];
 currentId: string;
 onSelect: (id: string) => void;
 onClose: () => void;
 onAddAccount?: () => void;
}

export function SwitchAccountDialog({
 open,
 accounts,
 currentId,
 onSelect,
 onClose,
 onAddAccount,
}: SwitchAccountDialogProps) {
 return (
   <Modal open={open} onClose={onClose} title="Chuyển đổi tài khoản" width={420}>
     <ul className={styles.list}>
       {accounts.map((a) => {
         const active = a.id === currentId;
         return (
           <li key={a.id}>
             <button
               type="button"
               className={[styles.row, active ? styles.active : ''].filter(Boolean).join(' ')}
               onClick={() => {
                 onSelect(a.id);
                 onClose();
               }}
               aria-current={active ? 'true' : undefined}
             >
               <Avatar initials={a.initials} size={36} />
               <span className={styles.meta}>
                 <span className={styles.name}>{a.name}</span>
                 {a.email && <span className={styles.email}>{a.email}</span>}
               </span>
               {active && <Check size={18} className={styles.check} />}
             </button>
           </li>
         );
       })}
     </ul>
     {onAddAccount && (
       <button type="button" className={styles.add} onClick={onAddAccount}>
         <span className={styles.addIcon}>
           <Plus size={16} />
         </span>
         Thêm tài khoản khác
       </button>
     )}
   </Modal>
 );
}