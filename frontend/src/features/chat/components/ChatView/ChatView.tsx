import type { Feedback, Message, MessageAttachment, UploadResult } from '../../../../types';
import { MessageList } from '../MessageList';
import { PromptComposer } from '../PromptComposer';
import { EmptyState } from '../EmptyState';
import styles from './ChatView.module.css';

interface ChatViewProps {
 messages: Message[];
 searchQuery?: string;
 activeSearchMessageId?: string | null;
 onSend?: (text: string, attachments?: MessageAttachment[]) => void;
 onRegenerate?: (messageId: string) => void;
 onUploadFiles?: (files: FileList | File[]) => Promise<UploadResult[]>;
 onFeedback?: (messageId: string, value: Feedback) => void;
}

export function ChatView({
 messages,
 searchQuery = '',
 activeSearchMessageId = null,
 onSend,
 onRegenerate,
 onUploadFiles,
 onFeedback,
}: ChatViewProps) {
 return (
   <div className={styles.view}>
     {messages.length === 0 ? (
       <EmptyState />
     ) : (
       <MessageList
         messages={messages}
         searchQuery={searchQuery}
         activeSearchMessageId={activeSearchMessageId}
         onRegenerate={onRegenerate}
         onFeedback={onFeedback}
       />
     )}
     <PromptComposer onSend={onSend} onUploadFiles={onUploadFiles} />
   </div>
 );
}
