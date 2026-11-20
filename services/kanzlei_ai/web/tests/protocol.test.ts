import { describe, it, expect } from 'vitest';
import { SSEParser, validTicketMessage } from '../src/protocol';

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
});
