import {
 useEffect,
 useRef,
 useState,
 type ChangeEvent,
 type ClipboardEvent,
 type DragEvent,
 type KeyboardEvent,
} from 'react';
import {
 ArrowUp,
 CheckCircle2,
 FileText,
 Image as ImageIcon,
 LoaderCircle,
 Paperclip,
 X,
 XCircle,
} from 'lucide-react';
import type { MessageAttachment, UploadResult } from '../../../../types';
import { documentFileUrl } from '../../../../shared/api/client';
import { IconButton } from '../../../../shared/ui/IconButton';
import styles from './PromptComposer.module.css';

const FILE_ACCEPT = '.pdf,.docx,.pptx,.txt,.md,.markdown,.rst,.csv,.html,.htm,.png,.jpg,.jpeg,.webp,.gif,.bmp,.tif,.tiff';
const IMAGE_ACCEPT = '.png,.jpg,.jpeg,.webp,.gif,.bmp,.tif,.tiff,image/png,image/jpeg,image/webp,image/gif,image/bmp,image/tiff';
const MAX_ATTACHMENTS = 5;

const isImageFile = (file: File) => (
 file.type.startsWith('image/') || /\.(png|jpe?g|webp|gif|bmp|tiff?)$/i.test(file.name)
);

interface ComposerAttachment {
 id: string;
 file: File;
 isImage: boolean;
 previewUrl?: string;
 status: 'uploading' | 'active' | 'failed';
 docId?: string;
 chunks?: number;
 error?: string;
}

interface PromptComposerProps {
 onSend?: (text: string, attachments?: MessageAttachment[]) => void;
 onUploadFiles?: (files: FileList | File[]) => Promise<UploadResult[]>;
 placeholder?: string;
}

export function PromptComposer({
 onSend,
 onUploadFiles,
 placeholder = 'Nhập tin nhắn...',
}: PromptComposerProps) {
 const [value, setValue] = useState('');
 const [attachments, setAttachments] = useState<ComposerAttachment[]>([]);
 const [attachmentError, setAttachmentError] = useState('');
 const [dragging, setDragging] = useState(false);
 const taRef = useRef<HTMLTextAreaElement>(null);
 const fileInputRef = useRef<HTMLInputElement>(null);
 const imageInputRef = useRef<HTMLInputElement>(null);
 const attachmentsRef = useRef(attachments);
 attachmentsRef.current = attachments;

 const readyAttachments = attachments.filter((item) => item.status === 'active');
 const isUploading = attachments.some((item) => item.status === 'uploading');
 const canSend = !isUploading && (value.trim().length > 0 || readyAttachments.length > 0);

 useEffect(() => {
   const el = taRef.current;
   if (!el) return;
   el.style.height = 'auto';
   el.style.height = `${Math.min(el.scrollHeight, 180)}px`;
 }, [value]);

 useEffect(() => () => {
   for (const attachment of attachmentsRef.current) {
     if (attachment.previewUrl) URL.revokeObjectURL(attachment.previewUrl);
   }
 }, []);

 const submit = () => {
   if (!canSend) return;
   const names = readyAttachments.map((item) => item.file.name);
   let prompt = value.trim();
   if (!prompt && names.length) {
     prompt = readyAttachments.every((item) => item.isImage)
       ? `Hãy phân tích hình ảnh đính kèm: ${names.join(', ')}`
       : `Hãy tóm tắt tệp đính kèm: ${names.join(', ')}`;
   }
   const messageAttachments = readyAttachments.flatMap((item): MessageAttachment[] => (
     item.docId ? [{
       docId: item.docId,
       name: item.file.name,
       kind: item.isImage ? 'image' : 'file',
       previewUrl: item.previewUrl,
       mimeType: item.file.type || undefined,
     }] : []
   ));
   onSend?.(prompt, messageAttachments);
   setValue('');
   // The sent message owns these preview URLs now. Do not revoke them while
   // the image is still visible in the transcript.
   setAttachments([]);
   setAttachmentError('');
 };

 const onKeyDown = (event: KeyboardEvent<HTMLTextAreaElement>) => {
   if (event.key === 'Enter' && !event.shiftKey) {
     event.preventDefault();
     submit();
   }
 };

 const updateAttachment = (id: string, changes: Partial<ComposerAttachment>) => {
   setAttachments((current) => current.map((item) => (
     item.id === id ? { ...item, ...changes } : item
   )));
 };

 const uploadFiles = async (selected: File[]) => {
   if (!selected.length) return;
   const available = Math.max(0, MAX_ATTACHMENTS - attachmentsRef.current.length);
   if (!available) {
     setAttachmentError(`Bạn chỉ có thể đính kèm tối đa ${MAX_ATTACHMENTS} tệp.`);
     return;
   }
   const existing = new Set(attachmentsRef.current.map((item) => (
     `${item.file.name}:${item.file.size}:${item.file.lastModified}`
   )));
   const files = selected
     .filter((file) => !existing.has(`${file.name}:${file.size}:${file.lastModified}`))
     .slice(0, available);
   if (!files.length) return;
   if (selected.length > files.length) {
     setAttachmentError(`Một số tệp bị trùng hoặc vượt quá giới hạn ${MAX_ATTACHMENTS} tệp.`);
   } else {
     setAttachmentError('');
   }

   const pending = files.map((file, index): ComposerAttachment => ({
     id: `${Date.now()}-${index}-${file.name}`,
     file,
     isImage: isImageFile(file),
     previewUrl: isImageFile(file) ? URL.createObjectURL(file) : undefined,
     status: 'uploading',
   }));
   setAttachments((current) => [...current, ...pending]);

   for (const attachment of pending) {
     try {
       if (!onUploadFiles) throw new Error('File upload is unavailable.');
       const [result] = await onUploadFiles([attachment.file]);
       if (!result || result.status === 'failed') {
         throw new Error(result?.error || 'The file could not be indexed.');
       }
       updateAttachment(attachment.id, {
         status: 'active',
         docId: result.docId,
         chunks: result.chunks,
         previewUrl: attachment.isImage ? documentFileUrl(result.docId) : undefined,
         error: undefined,
       });
       if (attachment.previewUrl) URL.revokeObjectURL(attachment.previewUrl);
     } catch (error) {
       updateAttachment(attachment.id, {
         status: 'failed',
         error: error instanceof Error ? error.message : String(error),
       });
     }
   }
 };

 const onFileChange = (event: ChangeEvent<HTMLInputElement>) => {
   if (event.target.files?.length) void uploadFiles(Array.from(event.target.files));
   event.target.value = '';
 };

 const onDrop = (event: DragEvent<HTMLDivElement>) => {
   event.preventDefault();
   setDragging(false);
   if (event.dataTransfer.files?.length) {
     void uploadFiles(Array.from(event.dataTransfer.files));
   }
 };

 const onPaste = (event: ClipboardEvent<HTMLTextAreaElement>) => {
   const images = Array.from(event.clipboardData.files).filter((file) => (
     isImageFile(file)
   ));
   if (images.length) {
     event.preventDefault();
     void uploadFiles(images);
   }
 };

 const removeAttachment = (id: string) => {
   setAttachments((current) => {
     const removed = current.find((item) => item.id === id);
     if (removed?.previewUrl) URL.revokeObjectURL(removed.previewUrl);
     return current.filter((item) => item.id !== id);
   });
 };

 return (
   <div className={styles.wrap}>
     <div className={styles.inner}>
       <div
         className={[styles.composer, dragging ? styles.dragging : ''].filter(Boolean).join(' ')}
         onDragEnter={(event) => { event.preventDefault(); setDragging(true); }}
         onDragOver={(event) => event.preventDefault()}
         onDragLeave={(event) => {
           if (!event.currentTarget.contains(event.relatedTarget as Node | null)) setDragging(false);
         }}
         onDrop={onDrop}
       >
         {attachments.length > 0 && (
           <div className={styles.attachments} aria-label="Tệp đính kèm">
             {attachments.map((attachment) => (
               <div
                 key={attachment.id}
                 className={[
                   styles.attachment,
                   attachment.status === 'failed' ? styles.attachmentFailed : '',
                 ].filter(Boolean).join(' ')}
                 title={attachment.error || attachment.file.name}
               >
                 {attachment.previewUrl ? (
                   <img src={attachment.previewUrl} alt="" />
                 ) : (
                   <span className={styles.fileThumb}><FileText size={17} /></span>
                 )}
                 <span className={styles.attachmentInfo}>
                   <strong>{attachment.file.name}</strong>
                   <small>
                     {attachment.status === 'uploading' && 'Đang tải lên và lập chỉ mục…'}
                     {attachment.status === 'active' && (
                       attachment.chunks
                         ? `${attachment.chunks} đoạn đã sẵn sàng`
                         : 'Đã lập chỉ mục và sẵn sàng'
                     )}
                     {attachment.status === 'failed' && (attachment.error || 'Tải lên thất bại')}
                   </small>
                 </span>
                 <span className={styles.attachmentStatus} aria-hidden>
                   {attachment.status === 'uploading' && <LoaderCircle className={styles.spin} size={15} />}
                   {attachment.status === 'active' && <CheckCircle2 size={15} />}
                   {attachment.status === 'failed' && <XCircle size={15} />}
                 </span>
                 <button
                   type="button"
                   className={styles.removeAttachment}
                   onClick={() => removeAttachment(attachment.id)}
                   aria-label={`Bỏ tệp ${attachment.file.name}`}
                 >
                   <X size={13} />
                 </button>
               </div>
             ))}
           </div>
         )}
         {dragging && <div className={styles.dropNotice}>Thả tệp để tải lên</div>}
         <textarea
           ref={taRef}
           rows={1}
           className={styles.textarea}
           placeholder={placeholder}
           value={value}
           onChange={(event) => setValue(event.target.value)}
           onKeyDown={onKeyDown}
           onPaste={onPaste}
         />
         {attachmentError && <p className={styles.attachmentError}>{attachmentError}</p>}
         <div className={styles.toolbar}>
           <div className={styles.tools}>
             <IconButton
               label="Upload files"
               onClick={() => fileInputRef.current?.click()}
               disabled={attachments.length >= MAX_ATTACHMENTS}
             >
               <Paperclip size={18} />
             </IconButton>
             <IconButton
               label="Upload pictures"
               onClick={() => imageInputRef.current?.click()}
               disabled={attachments.length >= MAX_ATTACHMENTS}
             >
               <ImageIcon size={18} />
             </IconButton>
             <input
               ref={fileInputRef}
               type="file"
               multiple
               className={styles.hiddenInput}
               accept={FILE_ACCEPT}
               onChange={onFileChange}
             />
             <input
               ref={imageInputRef}
               type="file"
               multiple
               className={styles.hiddenInput}
               accept={IMAGE_ACCEPT}
               onChange={onFileChange}
             />
           </div>
           <button
             type="button"
             className={[styles.send, canSend ? styles.on : ''].filter(Boolean).join(' ')}
             onClick={submit}
             disabled={!canSend}
             aria-label={isUploading ? 'Wait for attachments to finish uploading' : 'Send message'}
           >
             {isUploading ? <LoaderCircle className={styles.spin} size={17} /> : <ArrowUp size={18} />}
           </button>
         </div>
       </div>
       <p className={styles.hint}>AI có thể mắc lỗi. Hãy kiểm tra thông tin quan trọng.</p>
     </div>
   </div>
 );
}
