import { Sparkles } from 'lucide-react';
import styles from './EmptyState.module.css';

export function EmptyState() {
 return (
   <div className={styles.empty}>
     <div className={styles.icon}>
       <Sparkles size={22} />
     </div>
     <h1 className={styles.title}>Bạn muốn tìm gì trong tài liệu công ty?</h1>
     <p className={styles.subtitle}>
       Đặt câu hỏi — câu trả lời sẽ được trích dẫn từ tài liệu nội bộ.
     </p>
   </div>
 );
}
