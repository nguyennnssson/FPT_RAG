import { useEffect, useRef, useState } from 'react';
import { ChevronDown, ChevronUp, PanelLeft, Search, X } from 'lucide-react';
import type { Message } from '../../../types';
import { IconButton } from '../../../shared/ui/IconButton';
import { Avatar } from '../../../shared/ui/Avatar';
import { AccountMenu } from '../../../features/account/components/AccountMenu';
import { ChatOverview } from '../../../features/conversations/components/ChatOverview';
import styles from './TopHeader.module.css';

interface TopHeaderProps {
 title: string;
 userInitials: string;
 sidebarCollapsed?: boolean;
 onToggleSidebar?: () => void;
 messages: Message[];
 searchOpen?: boolean;
 searchQuery?: string;
 searchMatchCount?: number;
 searchMatchIndex?: number;
 onSearchToggle?: () => void;
 onSearchChange?: (query: string) => void;
 onSearchPrevious?: () => void;
 onSearchNext?: () => void;
 onSearchClose?: () => void;
 summary?: string;
 summaryLoading?: boolean;
 summaryError?: string;
 onSummarize?: () => void;
 onSwitchAccount?: () => void;
 onMemories?: () => void;
 onDeletedConversations?: () => void;
 onLogout?: () => void;
}

export function TopHeader({
 title,
 userInitials,
 sidebarCollapsed = false,
 onToggleSidebar,
 messages,
 searchOpen = false,
 searchQuery = '',
 searchMatchCount = 0,
 searchMatchIndex = 0,
 onSearchToggle,
 onSearchChange,
 onSearchPrevious,
 onSearchNext,
 onSearchClose,
 summary,
 summaryLoading,
 summaryError,
 onSummarize,
 onSwitchAccount,
 onMemories,
 onDeletedConversations,
 onLogout,
}: TopHeaderProps) {
 const [overviewOpen, setOverviewOpen] = useState(false);
 const titleWrapRef = useRef<HTMLDivElement>(null);
 const searchRef = useRef<HTMLInputElement>(null);

 useEffect(() => {
   if (!overviewOpen) return undefined;
   const closeOutside = (event: MouseEvent) => {
     if (!titleWrapRef.current?.contains(event.target as Node)) setOverviewOpen(false);
   };
   document.addEventListener('mousedown', closeOutside);
   return () => document.removeEventListener('mousedown', closeOutside);
 }, [overviewOpen]);

 useEffect(() => {
   if (searchOpen) window.requestAnimationFrame(() => searchRef.current?.focus());
 }, [searchOpen]);

 return (
   <header className={styles.header}>
     <IconButton
       label="Toggle history"
       className={[styles.menu, sidebarCollapsed ? styles.menuVisible : '']
         .filter(Boolean)
         .join(' ')}
       onClick={onToggleSidebar}
     >
       <PanelLeft size={18} />
     </IconButton>
     <div className={styles.titleWrap} ref={titleWrapRef}>
       <button
         type="button"
         className={styles.title}
         onClick={() => setOverviewOpen((open) => !open)}
         aria-expanded={overviewOpen}
         aria-haspopup="dialog"
       >
         <span>{title}</span>
         <ChevronDown
           size={16}
           className={[styles.chevron, overviewOpen ? styles.chevronOpen : '']
             .filter(Boolean).join(' ')}
         />
       </button>
       {overviewOpen && (
         <ChatOverview
           title={title}
           messages={messages}
           summary={summary}
           summaryLoading={summaryLoading}
           summaryError={summaryError}
           onSummarize={onSummarize}
           onClose={() => setOverviewOpen(false)}
         />
       )}
     </div>
     <div className={styles.spacer} />
     {searchOpen ? (
       <div className={styles.chatSearch} role="search">
         <Search size={16} aria-hidden />
         <input
           ref={searchRef}
           value={searchQuery}
           onChange={(event) => onSearchChange?.(event.target.value)}
           onKeyDown={(event) => {
             if (event.key === 'Enter') {
               event.preventDefault();
               if (event.shiftKey) onSearchPrevious?.();
               else onSearchNext?.();
             } else if (event.key === 'Escape') {
               onSearchClose?.();
             }
           }}
           placeholder="Find in this chat"
           aria-label="Find in this chat"
         />
         <span className={styles.matchCount} aria-live="polite">
           {searchMatchCount ? searchMatchIndex + 1 : 0} / {searchMatchCount}
         </span>
         <button type="button" onClick={onSearchPrevious} disabled={!searchMatchCount} aria-label="Previous match">
           <ChevronUp size={15} />
         </button>
         <button type="button" onClick={onSearchNext} disabled={!searchMatchCount} aria-label="Next match">
           <ChevronDown size={15} />
         </button>
         <button type="button" onClick={onSearchClose} aria-label="Close chat search">
           <X size={15} />
         </button>
       </div>
     ) : (
       <IconButton label="Find in this chat (Ctrl+F)" onClick={onSearchToggle}>
         <Search size={18} />
       </IconButton>
     )}
     <AccountMenu placement="bottom" onSwitchAccount={onSwitchAccount} onMemories={onMemories} onDeletedConversations={onDeletedConversations} onLogout={onLogout}>
       {(open) => (
         <button
           type="button"
           className={styles.avatarBtn}
           onClick={open}
           aria-haspopup="menu"
           aria-label="Menu tài khoản"
         >
           <Avatar initials={userInitials} />
         </button>
       )}
     </AccountMenu>
   </header>
 );
}
