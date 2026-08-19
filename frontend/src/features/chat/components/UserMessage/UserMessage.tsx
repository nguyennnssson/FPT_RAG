import type { Message } from '../../../../types';
import styles from './UserMessage.module.css';

interface UserMessageProps {
 message: Message;
}

export function UserMessage({ message }: UserMessageProps) {
 return (
   <div className={styles.message}>
     <div className={styles.block}>
       {message.attachments && message.attachments.length > 0 && (
         <div className={styles.attachments} aria-label="Tệp đính kèm">
           {message.attachments.map((attachment) => (
             attachment.kind === 'image' && attachment.previewUrl ? (
               <img
                 key={attachment.docId}
                 className={styles.image}
                 src={attachment.previewUrl}
                 alt={attachment.name}
               />
             ) : (
               <span key={attachment.docId} className={styles.file}>
                 {attachment.name}
               </span>
             )
           ))}
         </div>
       )}
       {message.content && <div className={styles.content}>{message.content}</div>}
     </div>
     <time className={styles.meta} dateTime={message.createdAt}>
       {formatTime(message.createdAt)}
     </time>
   </div>
 );
}

function formatTime(value: string): string {
 const date = new Date(value);
 if (Number.isNaN(date.getTime())) return '';
 return new Intl.DateTimeFormat(undefined, {
   hour: 'numeric',
   minute: '2-digit',
 }).format(date);
}
