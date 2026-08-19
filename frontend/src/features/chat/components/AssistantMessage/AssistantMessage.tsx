import { useEffect, useState } from 'react';
import {
 Check,
 Copy,
 LoaderCircle,
 RefreshCw,
 Sparkles,
 TerminalSquare,
 ThumbsDown,
 ThumbsUp,
 XCircle,
} from 'lucide-react';
import type { Feedback, Message } from '../../../../types';
import { MarkdownRenderer } from '../../../../shared/ui/markdown/MarkdownRenderer';
import { SourceCardRow } from '../../../sources/components/SourceCardRow';
import { useSources } from '../../../sources/context';
import styles from './AssistantMessage.module.css';

interface AssistantMessageProps {
 message: Message;
 onRegenerate?: (messageId: string) => void;
 regenerateDisabled?: boolean;
 onFeedback?: (messageId: string, value: Feedback) => void;
}

export function AssistantMessage({
 message,
 onRegenerate,
 regenerateDisabled = false,
 onFeedback,
}: AssistantMessageProps) {
 const { open } = useSources();
 const [copied, setCopied] = useState(false);
 const [now, setNow] = useState(() => Date.now());
 const isStreaming = message.status === 'streaming';
 const activities = message.activity ?? [];
 const visibleActivity = [...activities].reverse().find(
   (item) => item.status === 'error' || (isStreaming && item.status === 'running'),
 );
 const startedAt = Date.parse(message.createdAt);
 const elapsedMs = isStreaming && Number.isFinite(startedAt)
   ? Math.max(visibleActivity?.elapsedMs ?? 0, now - startedAt)
   : visibleActivity?.elapsedMs ?? 0;

 useEffect(() => {
   if (!isStreaming) return undefined;
   const timer = window.setInterval(() => setNow(Date.now()), 250);
   return () => window.clearInterval(timer);
 }, [isStreaming]);

 const openCitation = (index: string) => {
   const citation = message.citations?.find((c) => String(c.index) === index);
   const source = message.sources?.find((s) => s.id === citation?.sourceId);
   if (source) open(source);
 };

 const copy = async () => {
   try {
     await navigator.clipboard.writeText(message.content);
     setCopied(true);
     setTimeout(() => setCopied(false), 1500);
   } catch {
     /* clipboard unavailable */
   }
 };

 return (
   <div className={styles.message}>
     <div className={styles.row}>
       <div className={styles.logo} aria-hidden>
         <Sparkles size={16} />
       </div>
       <div className={styles.body}>
         {visibleActivity && (
           <div
             className={[
               styles.activity,
               visibleActivity.status === 'error' ? styles.activityError : '',
             ].filter(Boolean).join(' ')}
             role={visibleActivity.status === 'error' ? 'alert' : 'status'}
             aria-live="polite"
           >
             {visibleActivity.status === 'error' ? (
               <XCircle size={16} />
             ) : (
               <LoaderCircle className={styles.spin} size={16} />
             )}
             <div className={styles.activityBody}>
               <div className={styles.activityLabel}>
                 <span>{visibleActivity.label}</span>
                 <time>{formatElapsed(elapsedMs)}</time>
               </div>
               {visibleActivity.detail && (
                 visibleActivity.detailKind === 'command' ? (
                   <code className={styles.activityCommand}>
                     <TerminalSquare size={13} />
                     {visibleActivity.detail}
                   </code>
                 ) : (
                   <p className={styles.activityDetail}>{visibleActivity.detail}</p>
                 )
               )}
             </div>
           </div>
         )}
         {message.sources && message.sources.length > 0 && (
           <SourceCardRow sources={message.sources} />
         )}
         <MarkdownRenderer content={message.content} onCitation={openCitation} />
         {isStreaming && (
           <div className={styles.typing} aria-label="Đang trả lời">
             <span />
             <span />
             <span />
           </div>
         )}
         {!isStreaming && (
           <div className={styles.actions}>
             <button type="button" className={styles.actBtn} onClick={copy}>
               {copied ? <Check size={14} /> : <Copy size={14} />}
               {copied ? 'Copied' : 'Copy'}
             </button>
             <button
               type="button"
               className={styles.actBtn}
               onClick={() => onRegenerate?.(message.id)}
               disabled={regenerateDisabled || !onRegenerate}
             >
               <RefreshCw size={14} />
               Regenerate
             </button>
             <span className={styles.divider} />
             <button
               type="button"
               className={[styles.iconAct, message.feedback === 'like' ? styles.active : '']
                 .filter(Boolean)
                 .join(' ')}
               onClick={() => onFeedback?.(message.id, 'like')}
               aria-label="Câu trả lời hữu ích"
               aria-pressed={message.feedback === 'like'}
             >
               <ThumbsUp size={14} />
             </button>
             <button
               type="button"
               className={[styles.iconAct, message.feedback === 'dislike' ? styles.active : '']
                 .filter(Boolean)
                 .join(' ')}
               onClick={() => onFeedback?.(message.id, 'dislike')}
               aria-label="Câu trả lời chưa tốt"
               aria-pressed={message.feedback === 'dislike'}
             >
               <ThumbsDown size={14} />
             </button>
           </div>
         )}
       </div>
     </div>
   </div>
 );
}

function formatElapsed(milliseconds: number): string {
 if (milliseconds < 1000) return `${Math.max(0, Math.round(milliseconds))} ms`;
 return `${(milliseconds / 1000).toFixed(milliseconds < 10_000 ? 1 : 0)} s`;
}
