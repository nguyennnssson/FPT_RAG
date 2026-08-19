import { Check, Sparkles } from 'lucide-react';
import { Modal } from '../../../../shared/ui/Modal';
import { Button } from '../../../../shared/ui/Button';
import styles from './UpgradeDialog.module.css';

interface UpgradeDialogProps {
 open: boolean;
 onClose: () => void;
 onUpgrade?: () => void;
}

const FEATURES = [
 'Không giới hạn số câu hỏi mỗi ngày',
 'Tải lên tài liệu dung lượng lớn',
 'Trả lời nhanh hơn, ưu tiên hàng đợi',
 'Trích dẫn nâng cao & xuất báo cáo',
];

export function UpgradeDialog({ open, onClose, onUpgrade }: UpgradeDialogProps) {
 return (
   <Modal
     open={open}
     onClose={onClose}
     title="Nâng cấp gói"
     width={460}
     footer={
       <>
         <Button onClick={onClose}>Để sau</Button>
         <Button variant="primary" leftIcon={<Sparkles size={16} />} onClick={onUpgrade}>
           Nâng cấp Pro
         </Button>
       </>
     }
   >
     <p className={styles.lead}>Mở khoá toàn bộ khả năng của trợ lý tri thức.</p>
     <ul className={styles.features}>
       {FEATURES.map((f) => (
         <li key={f}>
           <Check size={16} />
           {f}
         </li>
       ))}
     </ul>
   </Modal>
 );
}