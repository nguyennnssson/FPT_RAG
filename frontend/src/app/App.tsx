import { useCallback, useEffect, useMemo, useState } from 'react';
import { AppLayout } from './layout/AppLayout';
import { TopHeader } from './layout/TopHeader';
import { HistorySidebar, type AppView } from '../features/conversations/components/HistorySidebar';
import { ChatView } from '../features/chat/components/ChatView';
import { DocumentsPage } from '../features/documents/components/DocumentsPage';
import { SourcesProvider } from '../features/sources/context';
import { SwitchAccountDialog, type Account } from '../features/account/components/SwitchAccountDialog';
import { ConfirmDialog } from '../shared/ui/ConfirmDialog';
import { MemoryDialog } from '../features/account/components/MemoryDialog';
import { DeletedConversationsDialog } from '../features/conversations/components/DeletedConversationsDialog';
import { useChat } from '../features/chat/hooks/useChat';
import { useDocuments } from '../features/documents/hooks/useDocuments';
import {
 DEMO_USER,
 deleteConversation as deleteConversationApi,
 listConversations,
 summarizeConversation,
 updateConversation,
} from '../shared/api/client';
import type { Conversation } from '../types';

const ACCOUNTS: Account[] = [
 { id: 'web-user', name: 'Quang Anh', initials: 'QA', email: 'quang.anh@acme.vn' },
 { id: 'minh-trang', name: 'Minh Trang', initials: 'MT', email: 'minh.trang@acme.vn' },
 { id: 'product-team', name: 'Product Team', initials: 'PT', email: 'product@acme.vn' },
];

const viewFromPath = (path: string): AppView =>
 path.startsWith('/documents') ? 'documents' : 'chat';

export function App() {
 const [conversations, setConversations] = useState<Conversation[]>([]);
 const [activeConversationId, setActiveConversationId] = useState<string | null>(null);
 const [sidebarOpen, setSidebarOpen] = useState(false);
 const [sidebarCollapsed, setSidebarCollapsed] = useState(false);
 const [view, setView] = useState<AppView>(() => viewFromPath(window.location.pathname));

 const [currentAccountId, setCurrentAccountId] = useState(ACCOUNTS[0].id);
 const [switchOpen, setSwitchOpen] = useState(false);
 const [logoutOpen, setLogoutOpen] = useState(false);
 const [memoriesOpen, setMemoriesOpen] = useState(false);
 const [deletedOpen, setDeletedOpen] = useState(false);
 const [chatSearchOpen, setChatSearchOpen] = useState(false);
 const [chatSearchQuery, setChatSearchQuery] = useState('');
 const [chatSearchIndex, setChatSearchIndex] = useState(0);
 const [summaries, setSummaries] = useState<Record<string, string>>({});
 const [summaryErrors, setSummaryErrors] = useState<Record<string, string>>({});
 const [summaryLoadingId, setSummaryLoadingId] = useState<string | null>(null);

 const user = useMemo(
   () => ACCOUNTS.find((a) => a.id === currentAccountId) ?? ACCOUNTS[0],
   [currentAccountId],
 );
 const apiUser = useMemo(
   () => ({ ...DEMO_USER, user_id: user.id }),
   [user.id],
 );

 const mergeConversation = useCallback((conversation: Conversation) => {
   setConversations((current) => {
     const exists = current.some((item) => item.id === conversation.id);
     const next = exists
       ? current.map((item) => (item.id === conversation.id ? conversation : item))
       : [conversation, ...current];
     return next.sort((a, b) => {
       if (Boolean(a.pinned) !== Boolean(b.pinned)) return a.pinned ? -1 : 1;
       return new Date(b.updatedAt).getTime() - new Date(a.updatedAt).getTime();
     });
   });
 }, []);

 const handleConversationCreated = useCallback((conversation: Conversation) => {
   mergeConversation(conversation);
   setActiveConversationId(conversation.id);
 }, [mergeConversation]);

 const { messages, send, regenerate, setFeedback } = useChat(activeConversationId, apiUser, {
   onConversationCreated: handleConversationCreated,
   onConversationUpdated: mergeConversation,
 });
 const { documents, addFiles, previewDocument, previewDocumentFile } = useDocuments(apiUser);
 const loadConversations = useCallback(async () => {
   try {
     setConversations(await listConversations(apiUser));
   } catch {
     setConversations([]);
   }
 }, [apiUser]);

 // Keep the view in sync with the URL (back/forward navigation).
 useEffect(() => {
   const onPop = () => setView(viewFromPath(window.location.pathname));
   window.addEventListener('popstate', onPop);
   return () => window.removeEventListener('popstate', onPop);
 }, []);

 // History is server-owned; a refresh or restart restores the sidebar.
 useEffect(() => {
   setActiveConversationId(null);
   void loadConversations();
 }, [loadConversations]);

 const navigate = (next: AppView) => {
   setView(next);
   setSidebarOpen(false);
   const path = next === 'documents' ? '/documents' : '/';
   if (window.location.pathname !== path) window.history.pushState(null, '', path);
 };

 const activeConversation = useMemo(
   () => conversations.find((c) => c.id === activeConversationId) ?? null,
   [conversations, activeConversationId],
 );

 const searchMatches = useMemo(() => {
   const needle = chatSearchQuery.toLocaleLowerCase();
   if (!needle) return [];
   return messages
     .filter((message) => {
       const sourceText = (message.sources ?? [])
         .map((source) => `${source.name} ${source.page ?? ''}`)
         .join(' ');
       return `${message.content} ${sourceText}`.toLocaleLowerCase().includes(needle);
     })
     .map((message) => message.id);
 }, [messages, chatSearchQuery]);

 useEffect(() => setChatSearchIndex(0), [chatSearchQuery, activeConversationId]);
 useEffect(() => {
   if (chatSearchIndex >= searchMatches.length) setChatSearchIndex(0);
 }, [chatSearchIndex, searchMatches.length]);

 useEffect(() => {
   const handleFind = (event: KeyboardEvent) => {
     if (view !== 'chat') return;
     if ((event.ctrlKey || event.metaKey) && event.key.toLocaleLowerCase() === 'f') {
       event.preventDefault();
       setChatSearchOpen(true);
     } else if (event.key === 'Escape' && chatSearchOpen) {
       setChatSearchOpen(false);
     }
   };
   window.addEventListener('keydown', handleFind);
   return () => window.removeEventListener('keydown', handleFind);
 }, [view, chatSearchOpen]);

 const moveSearch = (direction: 1 | -1) => {
   if (!searchMatches.length) return;
   setChatSearchIndex((current) => (
     (current + direction + searchMatches.length) % searchMatches.length
   ));
 };

 const handleSummarize = async () => {
   if (!activeConversationId || summaryLoadingId) return;
   const id = activeConversationId;
   setSummaryLoadingId(id);
   setSummaryErrors((current) => ({ ...current, [id]: '' }));
   try {
     const result = await summarizeConversation(id, apiUser);
     setSummaries((current) => ({ ...current, [id]: result.summary }));
   } catch (error) {
     setSummaryErrors((current) => ({
       ...current,
       [id]: error instanceof Error ? error.message : 'Could not summarize this chat.',
     }));
   } finally {
     setSummaryLoadingId((current) => (current === id ? null : current));
   }
 };

 const handleNewChat = () => {
   setActiveConversationId(null);
   navigate('chat');
 };
 const handleSelect = (id: string) => {
   setActiveConversationId(id);
   setSidebarOpen(false);
   navigate('chat');
 };

 const renameConversation = (id: string, title: string) => {
   setConversations((prev) => prev.map((c) => (c.id === id ? { ...c, title } : c)));
   void updateConversation(id, { title }, apiUser)
     .then(mergeConversation)
     .catch(loadConversations);
 };
 const togglePin = (id: string) => {
   const conversation = conversations.find((item) => item.id === id);
   if (!conversation) return;
   const pinned = !conversation.pinned;
   setConversations((prev) => prev.map((c) => (c.id === id ? { ...c, pinned } : c)));
   void updateConversation(id, { pinned }, apiUser)
     .then(mergeConversation)
     .catch(loadConversations);
 };
 const deleteConversation = (id: string) => {
   setConversations((prev) => prev.filter((c) => c.id !== id));
   setActiveConversationId((cur) => (cur === id ? null : cur));
   void deleteConversationApi(id, apiUser).catch(loadConversations);
 };

 const confirmLogout = () => {
   setLogoutOpen(false);
   // TODO: gọi logout thật ở đây
 };

 const toggleSidebar = () => {
   if (window.matchMedia('(max-width: 768px)').matches) {
     setSidebarOpen((open) => !open);
   } else {
     setSidebarCollapsed((collapsed) => !collapsed);
   }
 };

 const collapseSidebar = () => {
   if (window.matchMedia('(max-width: 768px)').matches) {
     setSidebarOpen(false);
   } else {
     setSidebarCollapsed(true);
   }
 };

 const account = {
   onSwitchAccount: () => setSwitchOpen(true),
   onMemories: () => setMemoriesOpen(true),
   onDeletedConversations: () => setDeletedOpen(true),
   onLogout: () => setLogoutOpen(true),
 };

 const headerTitle = activeConversation?.title ?? 'Cuộc trò chuyện mới';

 return (
   <SourcesProvider>
     <AppLayout
       view={view}
       sidebarOpen={sidebarOpen}
       sidebarCollapsed={sidebarCollapsed}
       onCloseSidebar={() => setSidebarOpen(false)}
       sidebar={
         <HistorySidebar
           conversations={conversations}
           activeId={activeConversationId}
           userName={user.name}
           userInitials={user.initials}
           view={view}
           documentsCount={documents.length}
           onSelect={handleSelect}
           onNewChat={handleNewChat}
           onNavigate={navigate}
           onToggleCollapse={collapseSidebar}
           onRename={renameConversation}
           onTogglePin={togglePin}
           onDelete={deleteConversation}
           {...account}
         />
       }
     >
       {view === 'chat' && (
         <TopHeader
           title={headerTitle}
           messages={messages}
           userInitials={user.initials}
           sidebarCollapsed={sidebarCollapsed}
           onToggleSidebar={toggleSidebar}
           searchOpen={chatSearchOpen}
           searchQuery={chatSearchQuery}
           searchMatchCount={searchMatches.length}
           searchMatchIndex={chatSearchIndex}
           onSearchToggle={() => setChatSearchOpen(true)}
           onSearchChange={setChatSearchQuery}
           onSearchPrevious={() => moveSearch(-1)}
           onSearchNext={() => moveSearch(1)}
           onSearchClose={() => setChatSearchOpen(false)}
           summary={activeConversationId ? summaries[activeConversationId] : undefined}
           summaryLoading={summaryLoadingId === activeConversationId}
           summaryError={activeConversationId ? summaryErrors[activeConversationId] : undefined}
           onSummarize={activeConversationId ? () => void handleSummarize() : undefined}
           {...account}
         />
       )}
       {view === 'documents' ? (
         <DocumentsPage
           documents={documents}
           onFiles={addFiles}
           onPreview={previewDocument}
           onPreviewFile={previewDocumentFile}
           sidebarCollapsed={sidebarCollapsed}
           onToggleSidebar={toggleSidebar}
         />
       ) : (
         <ChatView
           messages={messages}
           searchQuery={chatSearchOpen ? chatSearchQuery : ''}
           activeSearchMessageId={
             chatSearchOpen ? searchMatches[chatSearchIndex] ?? null : null
           }
           onSend={send}
           onRegenerate={regenerate}
           onUploadFiles={addFiles}
           onFeedback={setFeedback}
         />
       )}
     </AppLayout>

     <SwitchAccountDialog
       open={switchOpen}
       accounts={ACCOUNTS}
       currentId={currentAccountId}
       onSelect={setCurrentAccountId}
       onClose={() => setSwitchOpen(false)}
       onAddAccount={() => {
         setSwitchOpen(false);
         // TODO: mở luồng thêm/đăng nhập tài khoản thật
       }}
     />
     <MemoryDialog
       open={memoriesOpen}
       onClose={() => setMemoriesOpen(false)}
       user={apiUser}
     />
     <DeletedConversationsDialog
       open={deletedOpen}
       onClose={() => setDeletedOpen(false)}
       onRestore={mergeConversation}
       user={apiUser}
     />
     <ConfirmDialog
       open={logoutOpen}
       title="Đăng xuất?"
       message="Bạn sẽ cần đăng nhập lại để tiếp tục."
       confirmLabel="Đăng xuất"
       danger
       onConfirm={confirmLogout}
       onCancel={() => setLogoutOpen(false)}
     />
   </SourcesProvider>
 );
}
