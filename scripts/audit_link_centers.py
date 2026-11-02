"""Read-only review of an existing graph's central nodes using the current linker."""
import argparse
from collections import Counter
import json
from pathlib import Path
import sqlite3
import time

from remi.graph import active_connections
from remi.memory_linking import _memory_row, provider_plan, validate_plan
from remi.providers import providers_from_environment
from remi.settings import load_environment
from remi.storage import MemoryStore


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--env-file',type=Path,required=True)
    parser.add_argument('--centers',type=int,default=3)
    args=parser.parse_args()
    if args.output.exists():raise ValueError('fresh_output_required')
    store=MemoryStore()
    with sqlite3.connect(args.database.resolve().as_uri()+'?mode=ro',uri=True) as con:
        for table in ('memories','edges'):
            for (raw,) in con.execute('select record_json from '+table):
                row=json.loads(raw);store.put(table,row['owner'],row['id'],row)
    owner=Counter(row['owner'] for row in store.snapshot()['memories']).most_common(1)[0][0]
    memories={m.id:m for m in store.memories(owner)}
    edges=active_connections(store,owner,'9999-12-31T23:59:59+00:00')
    degrees=Counter(node for edge in edges for node in (edge.origin,edge.target))
    load_environment(args.env_file);model,_,_=providers_from_environment()
    results=[]
    for mid,degree in degrees.most_common(args.centers):
        neighbors=[e.target if e.origin==mid else e.origin for e in edges if mid in (e.origin,e.target)]
        rows=[_memory_row('N1',memories[mid])]
        rows.extend(_memory_row(f'M{i}',memories[neighbor]) for i,neighbor in enumerate(neighbors,1))
        state=dict(owner=owner,current_memories=rows[:1],recalled_memories=rows[1:],
            handles={row['handle']:row for row in rows},
            pairs=[dict(id=f'L{i}',left='N1',right=f'M{i}',allowed_evidence_ids=['E1']) for i in range(1,len(rows))],
            evidence=[dict(id='E1',source_id=memories[mid].current.source_ids[0],text=memories[mid].current.quote)],
            preserved_edges=[])
        raw=provider_plan(model,state,deadline=time.monotonic()+120)
        accepted,issues=validate_plan(raw,state)
        results.append(dict(memory_id=mid,degree=degree,memories=rows,output=raw,
            accepted_edges=[edge.model_dump() for edge in accepted],issues=issues))
        args.output.write_text(json.dumps(results,ensure_ascii=False,indent=2))
        print(json.dumps(dict(memory_id=mid,degree=degree,output=raw,issues=issues),ensure_ascii=False),flush=True)


if __name__=='__main__':main()
