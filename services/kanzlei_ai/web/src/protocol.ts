export type ChatScope = { kind: 'client'; client_id: string } | { kind: 'general' } | { kind: 'assistant' };

export function chatScopeOptionValue(scope: ChatScope) {
  return scope.kind === 'client' ? `client:${scope.client_id}` : scope.kind;
}

export function conversationCreateBody(scope: ChatScope) {
  return scope.kind === 'client' ? { client_id: scope.client_id } : { scope: { kind: scope.kind } };
}

export function conversationQuery(scope: ChatScope) {
  return new URLSearchParams(scope.kind === 'client'
    ? { client_id: scope.client_id }
    : { scope: scope.kind }).toString();
}

export function validTicketMessage(value: unknown, state: string): value is {type:string;state:string;ticket:string} {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return false;
  const v = value as Record<string, unknown>;
  return Object.keys(v).sort().join(',') === 'state,ticket,type' && v.type === 'kanzlei-ai:ticket' && v.state === state && typeof v.ticket === 'string' && v.ticket.length > 0 && v.ticket.length <= 4096;
}
export class SSEParser {
  private buffer = '';
  constructor(private onEvent: (event: string, data: unknown) => void) {}
  push(chunk: string) {
    this.buffer += chunk;
    if (this.buffer.length > 1_000_000) throw new Error('frame too large');
    let match: RegExpExecArray | null;
    while ((match = /\r?\n\r?\n/.exec(this.buffer))) {
      const frame = this.buffer.slice(0, match.index); this.buffer = this.buffer.slice(match.index + match[0].length);
      let event = 'message'; const data: string[] = [];
      for (const line of frame.split(/\r?\n/)) {
        if (line.startsWith('event:')) event = line.slice(6).trimStart();
        if (line.startsWith('data:')) data.push(line.slice(5).replace(/^ /, ''));
      }
      if (data.length) this.onEvent(event, JSON.parse(data.join('\n')));
    }
  }
}
export function randomBinding(): string {
  const bytes = crypto.getRandomValues(new Uint8Array(32));
  return btoa(String.fromCharCode(...bytes)).replace(/\+/g,'-').replace(/\//g,'_').replace(/=+$/,'');
}
export async function challenge(verifier: string): Promise<string> {
  const hash = new Uint8Array(await crypto.subtle.digest('SHA-256', new TextEncoder().encode(verifier)));
  return btoa(String.fromCharCode(...hash)).replace(/\+/g,'-').replace(/\//g,'_').replace(/=+$/,'');
}
