import { describe, it, expect } from 'vitest';
import { chatScopeOptionValue, conversationCreateBody, conversationQuery, SSEParser, validTicketMessage } from '../src/protocol';

describe('browser protocol', () => {
  it('parses split SSE frames and ignores partial data', () => {
    const events: unknown[] = [];
    const parser = new SSEParser((event, data) => events.push([event, data]));
    parser.push('event: status\r\ndata: {"phase":');
    expect(events).toEqual([]);
    parser.push('"retrieving"}\r\n\r\nevent: answer\ndata: {"id":"1"}\n\n');
    expect(events).toEqual([['status', {phase:'retrieving'}], ['answer', {id:'1'}]]);
  });
  it('accepts only the exact ticket schema and matching state', () => {
    expect(validTicketMessage({type:'kanzlei-ai:ticket',state:'a',ticket:'t'}, 'a')).toBe(true);
    expect(validTicketMessage({type:'kanzlei-ai:ticket',state:'b',ticket:'t'}, 'a')).toBe(false);
    expect(validTicketMessage({type:'kanzlei-ai:ticket',state:'a',ticket:'t',extra:1}, 'a')).toBe(false);
  });
  it('uses mutually exclusive general and legacy conversation selectors', () => {
    const general = { kind: 'general' } as const;
    const client = { kind: 'client', client_id: 'A +&/' } as const;

    expect(conversationCreateBody(general)).toEqual({ scope: { kind: 'general' } });
    expect(conversationQuery(general)).toBe('scope=general');
    expect(conversationCreateBody(client)).toEqual({ client_id: 'A +&/' });
    expect(conversationQuery(client)).toBe('client_id=A+%2B%26%2F');
  });
  it('creates and lists assistant conversations without a client', () => {
    const assistant = { kind: 'assistant' } as const;
    expect(conversationCreateBody(assistant)).toEqual({ scope: { kind: 'assistant' } });
    expect(conversationQuery(assistant)).toBe('scope=assistant');
    expect(chatScopeOptionValue({ kind: 'client', client_id: 'assistant' })).not.toBe(chatScopeOptionValue(assistant));
  });
  it('uses distinct option values even when a client ID equals the general option', () => {
    const general = chatScopeOptionValue({ kind: 'general' });
    expect(chatScopeOptionValue({ kind: 'client', client_id: general })).not.toBe(general);
  });
});
