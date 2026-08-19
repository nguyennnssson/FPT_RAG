import { useCallback, useEffect, useRef, useState } from 'react';
import { ArrowDown } from 'lucide-react';
import type { Feedback, Message } from '../../../../types';
import { AssistantMessage } from '../AssistantMessage';
import { UserMessage } from '../UserMessage';
import styles from './MessageList.module.css';

interface MessageListProps {
 messages: Message[];
 searchQuery?: string;
 activeSearchMessageId?: string | null;
 onRegenerate?: (messageId: string) => void;
 onFeedback?: (messageId: string, value: Feedback) => void;
}

interface HighlightRegistry {
 set: (name: string, value: unknown) => void;
 delete: (name: string) => void;
}

export function MessageList({
 messages,
 searchQuery = '',
 activeSearchMessageId = null,
 onRegenerate,
 onFeedback,
}: MessageListProps) {
 const scrollRef = useRef<HTMLDivElement>(null);
 const endRef = useRef<HTMLDivElement>(null);
 const [showScroll, setShowScroll] = useState(false);
 const responseInProgress = messages.some((message) => message.status === 'streaming');

 const scrollToBottom = useCallback((behavior: ScrollBehavior = 'smooth') => {
   endRef.current?.scrollIntoView({ behavior, block: 'end' });
 }, []);

 useEffect(() => {
   scrollToBottom('smooth');
 }, [messages.length, scrollToBottom]);

 useEffect(() => {
   if (!activeSearchMessageId) return;
   scrollRef.current
     ?.querySelector(`[data-message-id="${CSS.escape(activeSearchMessageId)}"]`)
     ?.scrollIntoView({ behavior: 'smooth', block: 'center' });
 }, [activeSearchMessageId]);

 useEffect(() => {
   const root = scrollRef.current;
   const registry = (CSS as unknown as { highlights?: HighlightRegistry }).highlights;
   const HighlightCtor = (window as unknown as {
     Highlight?: new (...ranges: Range[]) => unknown;
   }).Highlight;
   registry?.delete('chat-search');
   registry?.delete('chat-search-active');
   const needle = searchQuery.toLocaleLowerCase();
   if (!root || !registry || !HighlightCtor || !needle) return undefined;

   const matches: Range[] = [];
   const activeMatches: Range[] = [];
   const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
   let node = walker.nextNode();
   while (node) {
     const value = node.textContent ?? '';
     const lower = value.toLocaleLowerCase();
     let start = lower.indexOf(needle);
     while (start !== -1) {
       const range = new Range();
       range.setStart(node, start);
       range.setEnd(node, start + needle.length);
       const owner = node.parentElement?.closest<HTMLElement>('[data-message-id]');
       if (owner?.dataset.messageId === activeSearchMessageId) activeMatches.push(range);
       else matches.push(range);
       start = lower.indexOf(needle, start + Math.max(needle.length, 1));
     }
     node = walker.nextNode();
   }
   if (matches.length) registry.set('chat-search', new HighlightCtor(...matches));
   if (activeMatches.length) {
     registry.set('chat-search-active', new HighlightCtor(...activeMatches));
   }
   return () => {
     registry.delete('chat-search');
     registry.delete('chat-search-active');
   };
 }, [searchQuery, activeSearchMessageId, messages]);

 const onScroll = () => {
   const el = scrollRef.current;
   if (!el) return;
   const distance = el.scrollHeight - el.scrollTop - el.clientHeight;
   setShowScroll(distance > 120);
 };

 return (
   <div className={styles.container}>
     <div className={styles.scroll} ref={scrollRef} onScroll={onScroll}>
       <div className={styles.thread}>
         {messages.map((m) => (
           <div
             key={m.id}
             data-message-id={m.id}
             className={m.id === activeSearchMessageId ? styles.activeMatch : undefined}
           >
             {m.role === 'assistant' ? (
               <AssistantMessage
                 message={m}
                 onRegenerate={onRegenerate}
                 regenerateDisabled={responseInProgress}
                 onFeedback={onFeedback}
               />
             ) : (
               <UserMessage message={m} />
             )}
           </div>
         ))}
         <div ref={endRef} />
       </div>
     </div>
     {showScroll && (
       <button
         type="button"
         className={styles.scrollButton}
         onClick={() => scrollToBottom('smooth')}
         aria-label="Scroll to bottom"
       >
         <ArrowDown size={18} />
       </button>
     )}
   </div>
 );
}
