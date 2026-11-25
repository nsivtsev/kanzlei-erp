import {
  SSEParser, validTicketMessage, randomBinding, challenge,
  chatScopeOptionValue, conversationCreateBody, conversationQuery, type ChatScope,
} from './protocol';
import { renderMarkdown } from './markdown';
import { themeSwitcher } from './theme';
import './style.css';

type Client = { id: string; label: string };
type Citation = { client?: Client } & (
  | { id: string; title: string; page: number; source_kind?: 'pdf' }
  | { id: string; title: string; page: null; source_kind: 'email' | 'docx' });
type Item = {
  id: string;
  created_at: string;
  mode?: 'strict' | 'assistant';
  status: 'answered' | 'no_evidence' | 'unavailable';
  question: string | null;
  answer: string | null;
  citations: Citation[];
};
type Processing = 'pending' | 'ready' | 'failed';
type Source = { source_id: string; title: string; status: Processing };
type Filters = { query: string; status: 'all' | Processing };
type Conversation = { id: string; created_at: string }
  & ({ client_id: string } | { client_id: null; scope: { kind: 'general' | 'assistant' } });
type OwnedConversation = { id: string; created_at: string; scope_kind: 'client' | 'general' | 'assistant' };
type Grant = { access_token: string; token_type: string; identity: { expires_at: number; general_access?: unknown } };
type Lease = { token: string; expiry: number; erpOrigin: string; generalAccess: boolean; controller: AbortController };
type Page<T> = { items: T[]; next_cursor: string | null };
type Viewer = { citation: Citation; canvas: HTMLCanvasElement; pages: number; zoom: number };
type CitationRequest = { turn: string; citation: Citation; controller: AbortController; focus: string };
type ConversationManagement = {
  dialog: HTMLDialogElement;
  session: Lease;
  controller: AbortController;
  items: OwnedConversation[];
  next: string | null;
  confirming: OwnedConversation | null;
  loading: boolean;
  deleting: boolean;
  message: string;
};

const app = document.querySelector<HTMLDivElement>('#app')!;
const narrow = matchMedia('(max-width: 760px)');
const revalidationDelay = 30_000;
const apiBase = import.meta.env.BASE_URL.slice(0, -1);
const conversationsPath = '/v1/chat/conversations';
const maxPagedItems = 10_000;
const questionLimit = 4000, strictQuestionLimit = 1000;
const assistantScope: ChatScope = { kind: 'assistant' };
const maxPages = 1_000;
const sourceStatus: Record<Processing, string> = {
  pending: 'Ausstehend',
  ready: 'Bereit',
  failed: 'Fehlgeschlagen',
};

let lease: Lease | null = null;
let signedIn = false;
let expiryTimer = 0, generation = 0, loginGeneration = 0;
let popupCleanup: (() => void) | null = null;
let scopeController = new AbortController();
let clients: Client[] = [], conversations: Conversation[] = [], history: Item[] = [];
let chatScope: ChatScope | null = null;
let conversation = '', draft = '', status = '';
let askController: AbortController | null = null;
let activeQuestion = '';
let streamingAnswer = '';
let strictMode = false;
let historyCheck: object | null = null;
let accessCheck: number | null = null;
let revalidationTimer = 0;
let historyReady = false;
let citationRequest: CitationRequest | null = null;
let viewer: Viewer | null = null;
let pdfTask: import('pdfjs-dist').PDFDocumentLoadingTask | null = null;
let pdfRender: import('pdfjs-dist').RenderTask | null = null;
let management: ConversationManagement | null = null;
const deletedConversations = new Set<string>();

let catalogue = false;
let catalogueRequest: AbortController | null = null;
let cataloguePage: Page<Source> | null = null;
let catalogueTrail: (string | null)[] = [null];
let filters: Filters = { query: '', status: 'all' };
let catalogueError = false;

function el<K extends keyof HTMLElementTagNameMap>(tag: K, text?: string, cls?: string): HTMLElementTagNameMap[K] {
  const element = document.createElement(tag);
  if (text !== undefined) element.textContent = text;
  if (cls) element.className = cls;
  return element;
}

function button(text: string, action: () => void, cls = '', focus?: string) {
  const control = el('button', text, cls);
  control.type = 'button';
  control.onclick = action;
  if (focus) control.dataset.focus = focus;
  return control;
}

function current(session: Lease, sequence?: number) {
  return lease === session && !session.controller.signal.aborted
    && (sequence === undefined || generation === sequence);
}

function conversationPath(id = conversation) {
  return `${conversationsPath}/${encodeURIComponent(id)}`;
}

function setStatus(message: string) {
  status = message;
  const live = app.querySelector('#chat-status');
  if (live) live.textContent = status;
}

function clearViewer() {
  citationRequest?.controller.abort();
  citationRequest = null;
  pdfRender?.cancel();
  pdfRender = null;
  if (pdfTask) void pdfTask.destroy().catch(() => {});
  pdfTask = null;
  viewer = null;
}

function stopHistoryCheck() {
  historyReady = false;
  historyCheck = null;
}

function resetCatalogue() {
  catalogueRequest?.abort();
  catalogueRequest = null;
  catalogue = false;
  cataloguePage = null;
  catalogueTrail = [null];
  filters = { query: '', status: 'all' };
  catalogueError = false;
}

function resetConversation() {
  stopHistoryCheck();
  generation++;
  scopeController.abort();
  scopeController = new AbortController();
  askController?.abort();
  askController = null;
  activeQuestion = '';
  streamingAnswer = '';
  clearViewer();
  conversation = '';
  history = [];
  draft = '';
}

function resetScope() {
  resetCatalogue();
  resetConversation();
  chatScope = null;
  conversations = [];
}

function invalidateScope(scopeDenied = false) {
  if (scopeDenied && chatScope) {
    const denied = chatScope;
    if (denied.kind === 'general' && lease) lease.generalAccess = false;
    if (denied.kind === 'client') clients = clients.filter(client => client.id !== denied.client_id);
    resetScope();
  } else {
    const previous = conversation;
    resetCatalogue();
    resetConversation();
    conversation = previous;
  }
  status = 'Der Zugriff auf dieses Gespräch oder seine Quellen ist nicht mehr verfügbar.';
  render(scopeDenied ? 'scope' : undefined);
  void refreshAccess();
}

function logout(localOnly = false) {
  const previous = lease;
  loginGeneration++;
  popupCleanup?.();
  window.clearTimeout(expiryTimer);
  window.clearTimeout(revalidationTimer);
  lease = null;
  signedIn = false;
  previous?.controller.abort();
  closeConversationManagement(false);
  deletedConversations.clear();
  resetScope();
  clients = [];
  status = '';
  render();
  if (previous && !localOnly) {
    void fetch(`${apiBase}/v1/auth/logout`, {
      method: 'POST', credentials: 'omit', headers: { Authorization: `Bearer ${previous.token}` },
    }).catch(() => {});
  }
}

async function api(session: Lease, path: string, init: RequestInit = {}, sequence?: number) {
  if (!current(session, sequence)) throw Error('stale');
  if (Date.now() >= session.expiry) {
    logout(true);
    throw Error('expired');
  }
  const headers = new Headers(init.headers);
  headers.set('Authorization', `Bearer ${session.token}`);
  if (init.body) headers.set('Content-Type', 'application/json');
  const signals = [session.controller.signal];
  if (sequence !== undefined) signals.push(scopeController.signal);
  if (init.signal) signals.push(init.signal);
  const response = await fetch(apiBase + path, {
    ...init, credentials: 'omit', headers, signal: AbortSignal.any(signals),
  });
  if (!current(session, sequence) || init.signal?.aborted) throw Error('stale');
  if (response.status === 401) {
    logout(true);
    throw Error('expired');
  }
  if (!response.ok) {
    const pathname = path.split('?', 1)[0];
    if (sequence !== undefined && pathname.startsWith(conversationsPath)
        && [403, 404, 409, 410, 503].includes(response.status)) {
      // Citation and catalogue denials can concern one source rather than the selected scope.
      const scopeDenied = response.status === 403 && (pathname === conversationsPath
        || pathname.endsWith('/history') || pathname.endsWith('/turns'));
      invalidateScope(scopeDenied);
    }
    throw Error('request failed');
  }
  return response;
}

async function pages<T>(session: Lease, path: string, sequence?: number, post = false): Promise<T[]> {
  const items: T[] = [];
  let cursor: string | null = null;
  const seen = new Set<string>();
  do {
    const url = new URL(path, location.origin);
    if (cursor && !post) url.searchParams.set('cursor', cursor);
    const request: RequestInit = { cache: 'no-store' };
    if (post) {
      request.method = 'POST';
      request.body = JSON.stringify({ limit: 100, cursor });
    }
    const page = await (await api(session, url.pathname + url.search, request, sequence)).json() as Page<T>;
    if (!current(session, sequence)) throw Error('stale');
    items.push(...page.items);
    cursor = page.next_cursor;
    if (items.length > maxPagedItems || (cursor && (seen.size + 1 >= maxPages || seen.has(cursor)))) {
      throw Error('invalid pagination');
    }
    if (cursor) seen.add(cursor);
  } while (cursor);
  return items;
}

function applyAccess(session: Lease, sequence: number, update: { clients: Client[] } | { generalAccess: boolean }) {
  if (!current(session, sequence)) return;
  const scope = chatScope;
  if ('clients' in update) {
    if (JSON.stringify(clients) === JSON.stringify(update.clients)) return;
    clients = update.clients;
  } else {
    if (session.generalAccess === update.generalAccess) return;
    session.generalAccess = update.generalAccess;
  }
  if ((scope?.kind === 'general' && !session.generalAccess)
      || (scope?.kind === 'client' && !clients.some(client => client.id === scope.client_id))) {
    invalidateScope(true);
  } else {
    render();
  }
}

async function refreshAccess() {
  const session = lease, sequence = generation;
  if (!session || !signedIn || accessCheck === sequence) return;
  accessCheck = sequence;
  try {
    await Promise.allSettled([
      api(session, '/v1/auth/me', { cache: 'no-store' }, sequence)
        .then(response => response.json())
        .then(identity => applyAccess(session, sequence, { generalAccess: identity.general_access === true })),
      pages<Client>(session, '/v1/connector/clients', sequence, true)
        .then(list => applyAccess(session, sequence, { clients: list })),
    ]);
  } finally {
    if (accessCheck === sequence) accessCheck = null;
  }
}

async function login() {
  popupCleanup?.();
  const popup = window.open('about:blank', 'kanzlei-ai-connect', 'popup,width=600,height=720');
  if (!popup) {
    setStatus('Das Anmeldefenster wurde blockiert.');
    return;
  }
  const attempt = ++loginGeneration;
  lease?.controller.abort();
  closeConversationManagement(false);
  deletedConversations.clear();
  lease = null;
  signedIn = false;
  clients = [];
  resetScope();
  const state = randomBinding(), verifier = randomBinding();
  let done = false, timer = 0, poll = 0, origin = '';
  const cleanup = () => {
    if (done) return;
    done = true;
    window.clearTimeout(timer);
    window.clearInterval(poll);
    window.removeEventListener('message', onMessage);
    popup.close();
    if (popupCleanup === cleanup) popupCleanup = null;
  };
  const onMessage = async (event: MessageEvent) => {
    if (done || event.origin !== origin || event.source !== popup || !validTicketMessage(event.data, state)) return;
    const ticket = event.data.ticket;
    cleanup();
    try {
      const response = await fetch(`${apiBase}/v1/auth/browser/exchange`, {
        method: 'POST', credentials: 'omit', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ version: 1, ticket, state, verifier }),
      });
      if (!response.ok) throw Error();
      const grant = await response.json() as Grant;
      if (attempt !== loginGeneration) return;
      if (grant.token_type !== 'bearer' || typeof grant.access_token !== 'string'
          || !Number.isFinite(grant.identity?.expires_at) || grant.identity.expires_at * 1000 <= Date.now()) throw Error();
      const session: Lease = {
        token: grant.access_token, expiry: grant.identity.expires_at * 1000, erpOrigin: origin,
        generalAccess: grant.identity.general_access === true, controller: new AbortController(),
      };
      lease = session;
      window.clearTimeout(expiryTimer);
      expiryTimer = window.setTimeout(() => {
        if (current(session)) logout(true);
      }, Math.min(session.expiry - Date.now(), 2147483647));
      const list = await pages<Client>(session, '/v1/connector/clients', undefined, true);
      if (!current(session) || attempt !== loginGeneration) return;
      clients = list;
      signedIn = true;
      status = '';
      render('scope');
      scheduleRevalidation();
      // No Mandant is required: start in the broadest scope this account may use.
      void selectScope(session.generalAccess ? { kind: 'general' } : assistantScope);
    } catch {
      if (attempt === loginGeneration) {
        logout(true);
        setStatus('Anmeldung fehlgeschlagen. Bitte erneut versuchen.');
      }
    }
  };
  popupCleanup = cleanup;
  window.addEventListener('message', onMessage);
  timer = window.setTimeout(() => {
    cleanup();
    if (attempt === loginGeneration) setStatus('Anmeldung abgelaufen.');
  }, 60_000);
  poll = window.setInterval(() => {
    if (popup.closed) {
      cleanup();
      if (attempt === loginGeneration) setStatus('Anmeldung abgebrochen.');
    }
  }, 500);
  try {
    const [config, hash] = await Promise.all([
      fetch(`${apiBase}/v1/auth/browser/config`, { credentials: 'omit' }).then(response => {
        if (!response.ok) throw Error();
        return response.json();
      }),
      challenge(verifier),
    ]);
    if (done || attempt !== loginGeneration) return;
    const url = new URL(config.erp_origin);
    if (config.version !== 1 || url.origin !== config.erp_origin
        || !['http:', 'https:'].includes(url.protocol) || url.username || url.password) throw Error();
    origin = url.origin;
    const target = new URL('/kanzlei-ai-connect', origin);
    target.searchParams.set('state', state);
    target.searchParams.set('challenge', hash);
    popup.location.href = target.href;
  } catch {
    cleanup();
    if (attempt === loginGeneration) setStatus('Anmeldung derzeit nicht verfügbar.');
  }
}

async function selectScope(scope: ChatScope | null) {
  const session = lease;
  if (scope?.kind === 'general' && !session?.generalAccess) return;
  resetScope();
  chatScope = scope;
  status = '';
  render();
  const sequence = generation;
  if (!scope || !session) return;
  try {
    const list = await pages<Conversation>(session, `${conversationsPath}?${conversationQuery(scope)}`, sequence);
    if (!current(session, sequence)) return;
    conversations = list.filter(item => !deletedConversations.has(item.id));
    render();
  } catch {
    if (current(session, sequence)) setStatus('Gespräche konnten nicht geladen werden.');
  }
}

function scheduleRevalidation() {
  window.clearTimeout(revalidationTimer);
  if (signedIn && !document.hidden) {
    revalidationTimer = window.setTimeout(revalidateView, revalidationDelay);
  }
}

function revalidateView() {
  if (!signedIn || document.hidden) return;
  void refreshHistory();
  if (!historyCheck) void refreshAccess();
  scheduleRevalidation();
}

async function refreshHistory(initial = false) {
  const session = lease, id = conversation, sequence = generation;
  if (!session || !id || historyCheck || (!initial && (!historyReady || document.hidden
      || !history.some(item => item.status === 'answered')))) return;
  const request = {};
  historyCheck = request;
  const snapshot = new Set(history.map(item => item.id));
  try {
    const items = await pages<Item>(session, `${conversationPath(id)}/history`, sequence);
    if (!current(session, sequence) || historyCheck !== request) return;
    const returned = new Set(items.map(item => item.id));
    const recentlyAdded = history.filter(item => !snapshot.has(item.id) && !returned.has(item.id));
    const latest = [...items, ...recentlyAdded];
    const target = citationRequest;
    if (target && !latest.some(item => item.id === target.turn
        && item.status === 'answered' && item.citations.some(citation => citation.id === target.citation.id))) {
      clearViewer();
    }
    if (initial || JSON.stringify(latest) !== JSON.stringify(history)) {
      history = latest;
      render();
    }
  } catch {
    if (current(session, sequence) && historyCheck === request) {
      history = [];
      clearViewer();
      status = 'Verlauf ist derzeit nicht verfügbar.';
      render();
    }
  } finally {
    if (current(session, sequence) && historyCheck === request) {
      historyCheck = null;
      historyReady = true;
      void refreshAccess();
    }
  }
}

window.addEventListener('focus', revalidateView);
document.addEventListener('visibilitychange', () => {
  if (document.hidden) window.clearTimeout(revalidationTimer);
  else revalidateView();
});

async function selectConversation(id: string) {
  if (deletedConversations.has(id)) return;
  resetCatalogue();
  resetConversation();
  conversation = id;
  status = '';
  render('question');
  await refreshHistory(true);
}

async function newConversation() {
  const session = lease, sequence = generation, scope = chatScope;
  if (!session || !scope) return;
  try {
    const result = await (await api(session, conversationsPath, {
      method: 'POST', body: JSON.stringify(conversationCreateBody(scope)),
    }, sequence)).json() as Conversation;
    if (!current(session, sequence)) return;
    conversations = [result, ...conversations];
    await selectConversation(result.id);
  } catch {
    if (current(session, sequence)) setStatus('Gespräch konnte nicht erstellt werden.');
  }
}

function closeConversationManagement(restoreFocus = true) {
  const state = management;
  if (!state) return;
  management = null;
  state.controller.abort();
  state.dialog.close();
  state.dialog.remove();
  if (restoreFocus) app.querySelector<HTMLElement>('[data-focus="manage-conversations"]')?.focus();
}

function conversationDate(value: string) {
  return new Intl.DateTimeFormat('de-DE', { dateStyle: 'medium', timeStyle: 'short' })
    .format(new Date(value));
}

function renderConversationManagement(state: ConversationManagement, focus?: string) {
  const heading = el('h2', state.confirming ? 'Gespräch löschen?' : 'Gespräche verwalten');
  heading.id = 'conversation-management-heading';
  const actions = el('div', undefined, 'conversation-management-actions');
  const feedback = el('p', state.message, 'muted');
  feedback.setAttribute('role', 'status');
  feedback.hidden = !state.message;
  if (state.confirming) {
    const summary = el('p', `Das Gespräch vom ${conversationDate(state.confirming.created_at)} wird dauerhaft gelöscht.`);
    const cancel = button('Abbrechen', () => {
      const previous = state.confirming?.id;
      state.confirming = null;
      state.message = '';
      renderConversationManagement(state, previous);
    }, 'quiet', 'cancel-delete');
    const remove = button('Gespräch löschen', () => void deleteManagedConversation(state),
      'danger', 'confirm-delete');
    cancel.disabled = remove.disabled = state.deleting;
    actions.append(cancel, remove);
    state.dialog.replaceChildren(heading, summary, feedback, actions);
  } else {
    const list = el('ul', undefined, 'managed-conversations');
    for (const item of state.items) {
      const row = el('li');
      const details = el('div');
      details.append(el('strong', conversationDate(item.created_at)),
        el('small', item.scope_kind === 'general' ? 'Mandantenübergreifend' : 'Mandantengespräch'));
      const remove = button('Löschen', () => {
        state.confirming = item;
        state.message = '';
        renderConversationManagement(state, 'cancel-delete');
      }, 'quiet', item.id);
      remove.setAttribute('aria-label', `Löschen: ${conversationDate(item.created_at)}`);
      row.append(details, remove);
      list.append(row);
    }
    if (state.next) {
      const more = button('Weitere Gespräche', () => void loadManagedConversations(state, state.next), 'quiet', 'more-owned');
      more.disabled = state.loading;
      actions.append(more);
    }
    actions.append(button('Schließen', () => closeConversationManagement(), 'quiet', 'close-management'));
    state.dialog.replaceChildren(heading, list, feedback, actions);
  }
  if (focus) {
    state.dialog.querySelector<HTMLElement>(`[data-focus="${CSS.escape(focus)}"]`)?.focus();
  }
}

async function loadManagedConversations(state: ConversationManagement, cursor: string | null = null) {
  if (state.loading) return;
  state.loading = true;
  state.message = 'Gespräche werden geladen …';
  renderConversationManagement(state, cursor ? 'more-owned' : 'close-management');
  try {
    const path = `${conversationsPath}/owned${cursor ? `?cursor=${encodeURIComponent(cursor)}` : ''}`;
    const page = await (await api(state.session, path, {
      cache: 'no-store', signal: state.controller.signal,
    })).json() as Page<OwnedConversation>;
    if (management !== state) return;
    const items = cursor ? [...state.items, ...page.items] : page.items;
    state.items = items.filter(item => !deletedConversations.has(item.id));
    state.next = page.next_cursor;
    state.message = state.items.length ? '' : 'Keine gespeicherten Gespräche.';
  } catch {
    state.message = 'Gespräche konnten nicht geladen werden.';
  } finally {
    state.loading = false;
    if (management === state) renderConversationManagement(state,
      state.next && cursor ? 'more-owned' : 'close-management');
  }
}

async function deleteManagedConversation(state: ConversationManagement) {
  const id = state.confirming?.id;
  if (!id || state.deleting) return;
  state.deleting = true;
  state.message = 'Gespräch wird gelöscht …';
  if (conversation === id) {
    resetCatalogue();
    resetConversation();
    render();
  }
  renderConversationManagement(state);
  try {
    await api(state.session, conversationPath(id), { method: 'DELETE', signal: state.controller.signal });
    if (management !== state) return;
    deletedConversations.add(id);
    conversations = conversations.filter(item => item.id !== id);
    state.items = state.items.filter(item => item.id !== id);
    state.confirming = null;
    state.message = 'Gespräch gelöscht.';
    if (state.next === id) state.next = state.items.at(-1)?.id ?? null;
    render();
  } catch {
    state.message = 'Das Gespräch konnte nicht gelöscht werden.';
  } finally {
    state.deleting = false;
    if (management === state) renderConversationManagement(state,
      state.confirming ? 'cancel-delete' : 'close-management');
  }
}

function openConversationManagement() {
  if (!lease || management) return;
  clearViewer();
  render();
  const dialog = el('dialog', undefined, 'conversation-management');
  dialog.setAttribute('aria-labelledby', 'conversation-management-heading');
  const state: ConversationManagement = {
    dialog, session: lease, controller: new AbortController(),
    items: [], next: null, confirming: null, loading: false, deleting: false, message: '',
  };
  management = state;
  dialog.addEventListener('cancel', event => {
    event.preventDefault();
    if (!state.deleting) closeConversationManagement();
  });
  document.body.append(dialog);
  renderConversationManagement(state);
  dialog.showModal();
  void loadManagedConversations(state);
}

async function loadCatalogue(trail: (string | null)[], nextFilters = filters) {
  const session = lease, sequence = generation, scope = chatScope;
  if (!session || scope?.kind !== 'client') return;
  catalogueRequest?.abort();
  const request = new AbortController();
  catalogueRequest = request;
  catalogueError = false;
  cataloguePage = null;
  catalogueTrail = trail;
  filters = nextFilters;
  const cursor = trail.at(-1) ?? null;
  render();
  try {
    const result = await (await api(session, '/v1/chat/conversations/sources', {
      method: 'POST',
      body: JSON.stringify({ client_id: scope.client_id, cursor, ...nextFilters }),
      signal: request.signal,
    }, sequence)).json() as Page<Source>;
    if (!current(session, sequence) || catalogueRequest !== request) return;
    if (result.next_cursor && trail.includes(result.next_cursor)) throw Error();
    cataloguePage = result;
  } catch {
    if (current(session, sequence) && catalogueRequest === request) catalogueError = true;
  } finally {
    if (current(session, sequence) && catalogueRequest === request) {
      catalogueRequest = null;
      render();
    }
  }
}

function showCatalogue() {
  if (chatScope?.kind !== 'client') return;
  catalogue = true;
  clearViewer();
  void loadCatalogue([null]);
  app.querySelector<HTMLElement>('#catalogue-heading')?.focus();
}

function showChat() {
  resetCatalogue();
  render('question');
  revalidateView();
}

function showStreamingAnswer() {
  const target = app.querySelector<HTMLElement>('#streaming-answer');
  if (!target) return render();
  target.textContent = streamingAnswer;
  target.hidden = !streamingAnswer;
  const scroll = app.querySelector<HTMLElement>('.chat-scroll');
  if (scroll && scroll.scrollHeight - scroll.clientHeight - scroll.scrollTop < 120) scroll.scrollTop = scroll.scrollHeight;
}

async function ask(question: string) {
  const session = lease, id = conversation, sequence = generation;
  if (!session || !question.trim() || askController || !id) return;
  const strict = strictMode && chatScope?.kind !== 'assistant';
  if (question.length > (strict ? strictQuestionLimit : questionLimit)) {
    setStatus(`Die Frage ist zu lang (höchstens ${strict ? strictQuestionLimit : questionLimit} Zeichen).`);
    return;
  }
  const controller = new AbortController();
  askController = controller;
  activeQuestion = question;
  streamingAnswer = '';
  draft = '';
  status = strict ? 'Quellen werden geprüft …' : 'Antwort wird erstellt …';
  render('cancel-answer');
  let terminal = false;
  try {
    const response = await api(session, `${conversationPath(id)}/turns`, {
      method: 'POST', body: JSON.stringify({ question, strict }), signal: controller.signal,
    }, sequence);
    if (!response.headers.get('content-type')?.toLowerCase().startsWith('text/event-stream') || !response.body) throw Error();
    const parser = new SSEParser((event, data) => {
      if (!current(session, sequence) || controller.signal.aborted || terminal) return;
      if (event === 'status') {
        const phase = (data as { phase?: string }).phase;
        setStatus(phase === 'retrieving' ? (strict ? 'Quellen werden geprüft …' : 'Dokumente werden durchsucht …')
          : phase === 'generating' ? 'Antwort wird erstellt …'
            : phase === 'validating' ? 'Antwort wird geprüft …' : 'Verarbeitung läuft …');
      } else if (event === 'delta') {
        const text = (data as { text?: unknown })?.text;
        if (typeof text !== 'string') throw Error();
        streamingAnswer += text;
        if (streamingAnswer.length > 100_000) throw Error();
        showStreamingAnswer();
      } else if (event === 'reset') {
        // Text before a document search is not part of the final answer.
        streamingAnswer = '';
        showStreamingAnswer();
      } else if (event === 'answer') {
        const item = data as Item;
        if (!item || typeof item.id !== 'string'
            || !['answered', 'no_evidence', 'unavailable'].includes(item.status) || !Array.isArray(item.citations)) throw Error();
        terminal = true;
        history = [...history.filter(previous => previous.id !== item.id), item];
        status = '';
      } else if (event === 'error') {
        terminal = true;
        const code = (data as { code?: unknown })?.code;
        if (code === 'UNAUTHENTICATED') logout(true);
        else if (code === 'STALE_SOURCE' || code === 'FORBIDDEN' || code === 'NOT_FOUND') invalidateScope();
        else if (code === 'CHAT_UNAVAILABLE') {
          invalidateScope();
          setStatus('ERP oder Chat ist derzeit nicht erreichbar. Bitte erneut versuchen.');
        } else status = 'Die Antwort ist derzeit nicht verfügbar. Bitte erneut versuchen.';
      }
    });
    const reader = response.body.getReader(), decoder = new TextDecoder();
    try {
      while (!terminal) {
        const part = await reader.read();
        if (part.done) break;
        parser.push(decoder.decode(part.value, { stream: true }));
      }
      if (!terminal) throw Error();
    } finally {
      await reader.cancel().catch(() => {});
    }
  } catch {
    if (current(session, sequence)) {
      draft = question;
      status = controller.signal.aborted ? 'Anfrage abgebrochen.'
        : 'Die Antwort konnte nicht geladen werden. Bitte erneut versuchen.';
    }
  } finally {
    if (current(session, sequence) && askController === controller) {
      askController = null;
      activeQuestion = '';
      streamingAnswer = '';
      render(document.activeElement instanceof HTMLElement && document.activeElement.dataset.focus === 'cancel-answer'
        ? 'question' : undefined);
    }
  }
}

function originalTarget(lease: Lease, kind: 'email' | 'docx', locator: unknown) {
  if (typeof locator !== 'string' || !locator.startsWith('/') || /[#\\\x00-\x20\x7f]/.test(locator)) throw Error();
  const target = new URL(locator, lease.erpOrigin);
  if (target.origin !== lease.erpOrigin) throw Error();
  if (kind === 'email') {
    if (!/^\/desk\/communication\/(?:[A-Za-z0-9._~-]|%[0-9A-F]{2})+$/.test(locator)
        || target.pathname !== locator || decodeURIComponent(target.pathname).includes(lease.token)) throw Error();
    return target;
  }

  const path = locator.split('?', 1)[0];
  const params = target.searchParams;
  if (path !== target.pathname || params.size !== 3 || !params.get('customer') || !params.get('file_id')
      || [...params.values()].some(value => value.includes(lease.token))) throw Error();
  if ((path === '/api/method/kanzlei_erp.ai_connector.api.download_mail_attachment'
      || path === '/api/method/kanzlei_erp.ai_connector.api.download_drive_docx')
      && /^[0-9a-f]{64}$/.test(params.get('revision') ?? '')) return target;
  throw Error();
}

async function openCitation(turn: string, citation: Citation, focus: string) {
  clearViewer();
  const session = lease, sequence = generation;
  if (!session) return;
  const controller = new AbortController();
  const request = { turn, citation, controller, focus };
  citationRequest = request;
  status = citation.source_kind === 'email' ? 'E-Mail wird geprüft …'
    : citation.source_kind === 'docx' ? 'Word-Dokument wird geprüft …' : 'Dokument wird geladen …';
  render(focus);
  const valid = () => current(session, sequence) && citationRequest === request && !controller.signal.aborted;
  try {
    const path = `${conversationPath()}/turns/${encodeURIComponent(turn)}/citations/${encodeURIComponent(citation.id)}`;
    if (citation.source_kind === 'email' || citation.source_kind === 'docx') {
      const response = await api(session, `${path}/original`, {
        signal: controller.signal, redirect: 'error', cache: 'no-store',
      }, sequence);
      const { locator, source_kind: kind } = await response.json();
      if (!valid()) return;
      if (kind !== citation.source_kind) throw Error();
      const target = originalTarget(session, citation.source_kind, locator);
      if (kind === 'docx') {
        clearViewer();
        status = '';
        render(focus);
      }
      location.assign(target.href);
      return;
    }
    const pageNumber = citation.page;
    if (typeof pageNumber !== 'number' || !Number.isSafeInteger(pageNumber) || pageNumber < 1) throw Error();
    const response = await api(session, `${path}/pdf`, { signal: controller.signal }, sequence);
    if (!response.headers.get('content-type')?.toLowerCase().startsWith('application/pdf')
        || Number(response.headers.get('content-length')) > 25_000_000 || !response.body) throw Error();
    const reader = response.body.getReader();
    const parts: Uint8Array[] = [];
    let total = 0;
    try {
      while (true) {
        const part = await reader.read();
        if (part.done) break;
        total += part.value.length;
        if (total > 25_000_000 || !valid()) throw Error();
        parts.push(part.value);
      }
    } finally {
      await reader.cancel().catch(() => {});
    }
    if (!valid()) return;
    const data = new Uint8Array(total);
    let offset = 0;
    for (const part of parts) {
      data.set(part, offset);
      offset += part.length;
    }
    const [pdfjs, worker] = await Promise.all([
      import('pdfjs-dist'),
      import('pdfjs-dist/build/pdf.worker.min.mjs?url'),
    ]);
    if (!valid()) return;
    pdfjs.GlobalWorkerOptions.workerSrc = worker.default;
    pdfTask = pdfjs.getDocument({ data, enableXfa: false });
    const pdf = await pdfTask.promise;
    if (!valid()) return;
    const page = await pdf.getPage(pageNumber);
    if (!valid()) return;
    const canvas = el('canvas');
    canvas.id = 'pdf-canvas';
    canvas.setAttribute('aria-label', `Seite ${citation.page} aus ${citation.title}`);
    const viewport = page.getViewport({ scale: 1.5 });
    canvas.width = viewport.width;
    canvas.height = viewport.height;
    pdfRender = page.render({ canvas, canvasContext: canvas.getContext('2d')!, viewport, annotationMode: pdfjs.AnnotationMode.DISABLE });
    await pdfRender.promise;
    if (!valid()) return;
    pdfRender = null;
    viewer = { citation, canvas, pages: pdf.numPages, zoom: 100 };
    status = '';
    render(narrow.matches ? 'close-pdf' : undefined);
  } catch {
    if (valid()) {
      clearViewer();
      status = 'Die Quelle ist veraltet oder nicht verfügbar.';
      render(focus);
    }
  }
}

function citationFocus(turn: string, citation: Citation) {
  return `citation-${turn}-${citation.id}`;
}

function closeCitation() {
  const request = citationRequest;
  clearViewer();
  status = '';
  render(request?.focus);
}

function renderHeader() {
  const header = el('header', undefined, 'topbar');
  header.append(el('strong', 'Kanzlei AI', 'wordmark'));
  const generalAccess = lease?.generalAccess;
  if (signedIn) {
    const scope = el('div', undefined, 'scope');
    const label = el('label', 'Suchbereich');
    label.htmlFor = 'client';
    const select = el('select');
    select.id = 'client';
    select.dataset.focus = 'scope';
    if (!chatScope) select.append(new Option('Suchbereich auswählen', ''));
    const choices = clients.map<{ label: string; selection: ChatScope }>(({ id, label }) => ({
      label, selection: { kind: 'client', client_id: id },
    }));
    if (generalAccess) choices.unshift({ label: 'Alle berechtigten Mandanten', selection: { kind: 'general' } });
    choices.unshift({ label: 'Ohne Mandant', selection: assistantScope });
    const selections = new Map<string, ChatScope>();
    for (const { label, selection } of choices) {
      const value = chatScopeOptionValue(selection);
      selections.set(value, selection);
      select.append(new Option(label, value));
    }
    select.value = chatScope ? chatScopeOptionValue(chatScope) : '';
    select.onchange = () => void selectScope(selections.get(select.value) ?? null);
    scope.append(label, select);
    header.append(scope);
  }
  const actions = el('div', undefined, 'header-actions');
  actions.append(themeSwitcher());
  if (signedIn) actions.append(button('Abmelden', () => logout(), 'quiet', 'logout'));
  header.append(actions);
  return header;
}

function renderSidebar() {
  const aside = el('aside', undefined, 'sidebar');
  aside.setAttribute('aria-label', chatScope?.kind === 'client' ? 'Gespräche und Dokumente' : 'Gespräche');
  const create = button('+ Neues Gespräch', () => void newConversation(), 'new-chat', 'new-chat');
  create.setAttribute('aria-label', 'Neues Gespräch');
  create.disabled = !chatScope;
  aside.append(create, button('Gespräche verwalten', openConversationManagement,
    'quiet manage-conversations', 'manage-conversations'));
  if (chatScope) {
    const format = new Intl.DateTimeFormat('de-DE', { dateStyle: 'medium', timeStyle: 'short' });
    if (narrow.matches && conversations.length) {
      const label = el('label', 'Gespräche');
      label.htmlFor = 'conversation';
      const list = el('select');
      list.id = 'conversation';
      list.dataset.focus = 'conversation';
      list.append(new Option('Gespräch auswählen', ''));
      for (const entry of conversations) list.append(new Option(format.format(new Date(entry.created_at)), entry.id));
      list.value = conversation;
      list.onchange = () => void selectConversation(list.value);
      aside.append(label, list);
    } else if (conversations.length) {
      const title = el('h2', 'Gespräche', 'conversation-label');
      const list = el('ul', undefined, 'conversation-list');
      for (const entry of conversations) {
        const row = el('li');
        const control = button(format.format(new Date(entry.created_at)), () => void selectConversation(entry.id), 'quiet');
        if (entry.id === conversation && !catalogue) control.setAttribute('aria-current', 'page');
        row.append(control);
        list.append(row);
      }
      aside.append(title, list);
    }
  }
  if (chatScope?.kind === 'client') {
    const navigation = el('nav', undefined, 'sidebar-nav');
    if (conversation) {
      const chat = button('Zum Gespräch', showChat, 'quiet');
      chat.hidden = !catalogue;
      navigation.append(chat);
    }
    const documents = button('Dokumente', showCatalogue, 'quiet', 'documents');
    if (catalogue) documents.setAttribute('aria-current', 'page');
    navigation.append(documents);
    aside.append(navigation);
  }
  return aside;
}

function citationLabel(citation: Citation) {
  const clientName = chatScope?.kind === 'general' ? citation.client?.label : undefined;
  const action = citation.source_kind === 'email' ? 'E-Mail im ERP öffnen'
    : citation.source_kind === 'docx' ? 'Word-Dokument herunterladen'
      : `Seite ${citation.page}`;
  return clientName ? `${clientName} · ${action}` : action;
}

function citationMarker(item: Item, citation: Citation, number: number) {
  const focus = `inline-${citationFocus(item.id, citation)}`;
  const marker = button(`[${number}]`, () => void openCitation(item.id, citation, focus), 'citation-marker', focus);
  marker.setAttribute('aria-label', `Beleg ${number}: ${citation.title}, ${citationLabel(citation)}`);
  return marker;
}

function renderTurn(item: Item) {
  const article = el('article', undefined, 'turn');
  if (item.status === 'unavailable') {
    article.append(el('p', 'Dieser Beitrag ist nicht mehr verfügbar.', 'turn-notice'));
    return article;
  }
  article.append(el('p', item.question || '', 'question'));
  if (item.status === 'no_evidence') {
    article.append(el('p', item.answer || 'Keine ausreichenden Belege gefunden. Versuchen Sie es mit einem Dokumentnamen, einer Nummer oder einem Zeitraum.', 'turn-notice'));
    return article;
  }
  const assistant = item.mode === 'assistant';
  if (assistant) {
    const grounded = item.citations.length > 0;
    article.append(el('p', grounded ? 'Antwort aus Kanzlei-Dokumenten'
      : 'Allgemeine Antwort · nicht aus Kanzlei-Dokumenten', `answer-mode ${grounded ? 'grounded' : 'general'}`));
    const answer = el('div', undefined, 'answer markdown');
    answer.append(renderMarkdown(item.answer || '', number => {
      const citation = item.citations[number - 1];
      return citation ? citationMarker(item, citation, number) : null;
    }));
    article.append(answer);
  } else {
    const answer = el('p', item.answer || '', 'answer');
    if (item.citations.length === 1) answer.append(' ', citationMarker(item, item.citations[0], 1));
    article.append(answer);
  }
  const list = el('ul', undefined, 'citations');
  for (const [index, citation] of item.citations.entries()) {
    const row = el('li');
    const focus = citationFocus(item.id, citation);
    const control = button('', () => void openCitation(item.id, citation, focus), 'citation', focus);
    if (assistant) control.append(el('span', `[${index + 1}]`, 'citation-number'));
    control.setAttribute('aria-label', `${citation.title} · ${citationLabel(citation)}`);
    if (citationRequest?.turn === item.id && citationRequest.citation.id === citation.id) {
      control.setAttribute('aria-busy', String(!viewer));
      if (citation.page !== null) control.setAttribute('aria-expanded', String(!!viewer));
    }
    control.append(el('span', citation.title, 'citation-title'), el('small', citationLabel(citation), 'citation-page'));
    row.append(control);
    list.append(row);
  }
  if (list.children.length) article.append(list);
  return article;
}

function liveStatus() {
  const live = el('p', status, 'status');
  live.id = 'chat-status';
  live.setAttribute('role', 'status');
  live.setAttribute('aria-atomic', 'true');
  return live;
}

function renderChat() {
  const chat = el('section', undefined, 'chat');
  chat.setAttribute('aria-label', 'Chat');
  const body = el('div', undefined, 'chat-scroll');
  const content = el('div', undefined, 'chat-column');
  const named = history.find(item => item.status === 'answered' && item.citations.length)?.citations[0]?.title;
  content.append(el('h1', named?.replace(/\.pdf$/i, '') || (conversation ? 'Gespräch' : 'KI-Assistent')));
  const documents = chatScope && chatScope.kind !== 'assistant';
  if (!conversation) {
    const introduction = chatScope ? 'Beginnen Sie ein neues Gespräch oder öffnen Sie den Verlauf.'
      : 'Wählen Sie einen Suchbereich.';
    content.append(el('p', introduction, 'muted'));
  } else if (!history.length && !askController) {
    content.append(el('p', documents
      ? 'Stellen Sie eine allgemeine Frage oder eine Frage zu den Dokumenten in diesem Suchbereich.'
      : 'Stellen Sie eine allgemeine Frage. Für Antworten aus Dokumenten wählen Sie einen Mandanten als Suchbereich.', 'muted'));
    const suggestions = el('div', undefined, 'suggestions');
    const examples = documents
      ? ['Wie hoch ist der Rechnungsbetrag?', 'Fasse die letzte E-Mail zusammen.']
      : ['Was ist der Vorsteuerabzug?', 'Formuliere eine höfliche Erinnerung an fehlende Belege.'];
    for (const example of examples) {
      suggestions.append(button(example, () => {
        draft = example;
        const field = document.querySelector<HTMLTextAreaElement>('#question')!;
        field.value = example;
        field.focus();
      }, 'quiet'));
    }
    content.append(suggestions);
  }
  for (const item of history) content.append(renderTurn(item));
  if (askController) {
    const pending = el('div', undefined, 'turn');
    pending.append(el('p', activeQuestion, 'question'));
    const streaming = el('div', streamingAnswer, 'answer streaming');
    streaming.id = 'streaming-answer';
    streaming.hidden = !streamingAnswer;
    pending.append(streaming);
    content.append(pending);
  }
  body.append(content);
  chat.append(body);
  const footer = el('div', undefined, 'composer');
  footer.append(liveStatus());
  if (citationRequest && !viewer) footer.append(button('Laden abbrechen', closeCitation, 'quiet'));
  if (conversation) {
    const form = el('form');
    const label = el('label', 'Ihre Frage');
    label.htmlFor = 'question';
    const field = el('textarea');
    field.id = 'question';
    field.dataset.focus = 'question';
    field.required = true;
    field.maxLength = questionLimit;
    field.rows = 2;
    field.placeholder = documents ? 'Frage stellen – allgemein oder zu den Dokumenten …' : 'Frage stellen …';
    field.value = draft;
    field.readOnly = !!askController;
    field.oninput = () => { draft = field.value; };
    form.append(label, field);
    if (documents) {
      const option = el('label', undefined, 'strict-option');
      const box = el('input');
      box.type = 'checkbox';
      box.id = 'strict';
      box.dataset.focus = 'strict';
      box.checked = strictMode;
      box.disabled = !!askController;
      box.onchange = () => { strictMode = box.checked; };
      option.append(box, ' Nur wörtliche Belege');
      option.title = 'Antwortet nur mit einer wörtlichen Stelle oder einem geprüften Rechnungsbetrag.';
      form.append(option);
    }
    if (askController) {
      form.append(button('Abbrechen', () => askController?.abort(), 'quiet', 'cancel-answer'));
    } else {
      const submit = el('button', 'Senden', 'primary');
      submit.type = 'submit';
      form.append(submit);
      field.onkeydown = event => {
        if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) {
          event.preventDefault();
          form.requestSubmit();
        }
      };
    }
    form.onsubmit = event => {
      event.preventDefault();
      void ask(field.value);
    };
    footer.append(form);
  }
  chat.append(footer);
  return chat;
}

function renderCatalogue() {
  const section = el('section', undefined, 'catalogue');
  const heading = el('h1', 'Dokumente');
  heading.id = 'catalogue-heading';
  heading.tabIndex = -1;
  section.append(heading, liveStatus());
  const form = el('form', undefined, 'catalogue-filters');
  const query = el('input');
  query.id = 'document-query';
  query.dataset.focus = 'document-query';
  query.type = 'search';
  query.maxLength = 200;
  query.value = filters.query;
  const queryLabel = el('label', 'Dateiname');
  queryLabel.htmlFor = query.id;
  const state = el('select');
  state.id = 'document-status';
  state.dataset.focus = 'document-status';
  state.append(new Option('Alle Status', 'all'));
  for (const [value, label] of Object.entries(sourceStatus)) state.append(new Option(label, value));
  state.value = filters.status;
  const statusLabel = el('label', 'Status');
  statusLabel.htmlFor = state.id;
  const submit = el('button', 'Suchen', 'primary');
  submit.type = 'submit';
  submit.dataset.focus = 'document-search';
  const queryField = el('div'), statusField = el('div');
  queryField.append(queryLabel, query);
  statusField.append(statusLabel, state);
  form.append(queryField, statusField, submit);
  form.onsubmit = event => {
    event.preventDefault();
    void loadCatalogue([null], { query: query.value.trim(), status: state.value as Filters['status'] });
  };
  section.append(form);
  const feedback = el('p', undefined, 'muted');
  feedback.setAttribute('role', 'status');
  if (catalogueRequest) feedback.textContent = 'Dokumente werden geladen …';
  else if (catalogueError) feedback.textContent = 'Dokumente konnten nicht geladen werden.';
  else if (cataloguePage && !cataloguePage.items.length) feedback.textContent = cataloguePage.next_cursor
    ? 'Keine Treffer auf dieser Seite.' : 'Keine weiteren passenden Dokumente.';
  section.append(feedback);
  if (catalogueError) section.append(button('Erneut versuchen', () => void loadCatalogue(catalogueTrail)));
  if (cataloguePage?.items.length) {
    const table = el('table');
    const headers = el('tr');
    for (const name of ['Dateiname', 'Status']) {
      const header = el('th', name);
      header.scope = 'col';
      headers.append(header);
    }
    table.createTHead().append(headers);
    const body = table.createTBody();
    for (const item of cataloguePage.items) {
      const row = body.insertRow();
      row.insertCell().textContent = item.title;
      row.insertCell().append(el('span', sourceStatus[item.status], `processing ${item.status}`));
    }
    section.append(table);
  }
  const pager = el('div', undefined, 'pager');
  pager.append(button('Zurück', () => void loadCatalogue(catalogueTrail.slice(0, -1)), 'quiet', 'previous-page'));
  (pager.firstElementChild as HTMLButtonElement).disabled = catalogueTrail.length === 1 || !!catalogueRequest;
  const next = cataloguePage?.next_cursor;
  const forward = button('Weiter', () => { if (next) void loadCatalogue([...catalogueTrail, next]); }, 'quiet', 'next-page');
  forward.disabled = !next || !!catalogueRequest;
  pager.append(forward);
  section.append(pager);
  return section;
}

function renderViewer(currentViewer: Viewer) {
  const dialog = narrow.matches ? el('dialog') : el('section');
  dialog.className = 'viewer';
  dialog.setAttribute('aria-label', 'Dokumentansicht');
  if (dialog instanceof HTMLDialogElement) {
    dialog.oncancel = event => { event.preventDefault(); closeCitation(); };
  }
  const { citation, pages, canvas } = currentViewer;
  const header = el('div', undefined, 'viewer-heading');
  const title = el('h2', citation.title);
  const close = button('×', closeCitation, 'quiet icon-button', 'close-pdf');
  close.setAttribute('aria-label', 'Dokument schließen');
  header.append(title, close);
  const toolbar = el('div', undefined, 'viewer-toolbar');
  const counter = el('span', `Seite ${citation.page} / ${pages}`, 'mono');
  const zoomOut = button('−', () => zoom(-25), 'quiet icon-button', 'zoom-out');
  zoomOut.setAttribute('aria-label', 'Verkleinern');
  const zoomIn = button('+', () => zoom(25), 'quiet icon-button', 'zoom-in');
  zoomIn.setAttribute('aria-label', 'Vergrößern');
  const sheet = el('div', undefined, 'pdf-scroll');
  const syncZoom = () => {
    canvas.style.width = `${currentViewer.zoom}%`;
    zoomOut.disabled = currentViewer.zoom <= 75;
    zoomIn.disabled = currentViewer.zoom >= 200;
  };
  function zoom(increment: number) {
    currentViewer.zoom += increment;
    syncZoom();
  }
  syncZoom();
  toolbar.append(counter, zoomOut, zoomIn);
  sheet.append(canvas);
  dialog.append(header, toolbar, sheet);
  return dialog;
}

function render(focus?: string) {
  const active = document.activeElement;
  const previous = active instanceof HTMLElement ? active.dataset.focus : undefined;
  const previousField = active instanceof HTMLTextAreaElement || (active instanceof HTMLInputElement && active.type === 'search');
  const selection = previousField ? [active.selectionStart, active.selectionEnd] as const : null;
  const scroll = app.querySelector<HTMLElement>('.chat-scroll');
  const pdfScroll = app.querySelector<HTMLElement>('.pdf-scroll');
  const chatScrollTop = scroll?.scrollTop;
  const scrollToBottom = scroll && scroll.scrollHeight - scroll.clientHeight - scroll.scrollTop < 40;
  const pdfPosition = pdfScroll ? [pdfScroll.scrollLeft, pdfScroll.scrollTop] as const : null;

  const skip = el('a', 'Zum Inhalt', 'skip');
  skip.href = '#main';
  const main = el('main');
  main.id = 'main';
  main.tabIndex = -1;
  let modal: HTMLDialogElement | null = null;

  if (!signedIn) {
    const intro = el('section', undefined, 'welcome');
    intro.append(el('h1', 'KI-Assistent der Kanzlei'), el('p', 'Stellen Sie allgemeine Fragen oder Fragen zu den freigegebenen Dokumenten und prüfen Sie Antworten aus Dokumenten an der Quelle.', 'muted'));
    intro.append(button('Mit ERP anmelden', () => void login(), 'primary', 'login'), liveStatus());
    main.append(intro);
  } else {
    const layout = el('div', undefined, 'layout');
    if (viewer && !catalogue && !narrow.matches) layout.classList.add('with-viewer');
    layout.append(renderSidebar(), catalogue ? renderCatalogue() : renderChat());
    if (viewer && !catalogue) {
      const panel = renderViewer(viewer);
      layout.append(panel);
      if (panel instanceof HTMLDialogElement) modal = panel;
    }
    main.append(layout);
  }

  app.replaceChildren(skip, renderHeader(), main);
  if (modal) modal.showModal();
  const newScroll = app.querySelector<HTMLElement>('.chat-scroll');
  if (newScroll && chatScrollTop !== undefined) {
    newScroll.scrollTop = scrollToBottom ? newScroll.scrollHeight : chatScrollTop;
  }
  if (pdfPosition) app.querySelector<HTMLElement>('.pdf-scroll')?.scrollTo(...pdfPosition);
  const target = focus ?? previous;
  if (target) {
    const control = app.querySelector<HTMLElement>(`[data-focus="${CSS.escape(target)}"]`);
    if (control && !control.matches(':disabled')) {
      control.focus({ preventScroll: true });
      if (!focus && selection && (control instanceof HTMLTextAreaElement || control instanceof HTMLInputElement)) {
        control.setSelectionRange(...selection);
      }
    }
  }
}

narrow.addEventListener('change', () => {
  render(viewer && narrow.matches ? 'close-pdf' : undefined);
});
render();
