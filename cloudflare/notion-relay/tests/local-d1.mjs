import {DatabaseSync} from 'node:sqlite';
import {readFile} from 'node:fs/promises';

export class LocalD1 {
  constructor() { this.sqlite = new DatabaseSync(':memory:'); }
  prepare(sql) {
    const stmt=this.sqlite.prepare(sql);
    const bound=args=>({
      first:async()=>stmt.get(...args)??null,
      all:async()=>({results:stmt.all(...args)}),
      run:async()=>({success:true,meta:{changes:Number(stmt.run(...args).changes)}}),
      _execute:()=>({success:true,meta:{changes:Number(stmt.run(...args).changes)}}),
    });
    return {bind:(...args)=>bound(args),...bound([])};
  }
  async batch(queries) {
    this.sqlite.exec('BEGIN IMMEDIATE');
    try {const results=queries.map(q=>q._execute());this.sqlite.exec('COMMIT');return results;}
    catch(e){this.sqlite.exec('ROLLBACK');throw e;}
  }
}
export async function fixture(mode='ACTIVE') {
  const db=new LocalD1();
  for(const name of ['0001_journal.sql','0002_bootstrap.sql','0003_requests.sql'])
    db.sqlite.exec(await readFile(new URL(`../migrations/${name}`,import.meta.url),'utf8'));
  const env={DB:db,RELAY_MODE:mode,HELPER_REQUEST_KEY:'synthetic-helper-key-not-a-real-key-0000'};
  return {db,env};
}
