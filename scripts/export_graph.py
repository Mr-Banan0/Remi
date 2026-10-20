"""Export immutable evidence and version-bound graph projections through read-only SQLite."""
from __future__ import annotations
import argparse
import json
import sqlite3
from collections import Counter
from pathlib import Path

from remi.models import Memory
from remi.heat import visible_memory
from remi.clock import in_view, parse_time


def export(database, output):
    if output.exists():
        raise FileExistsError('fresh_graph_output_required')
    connection=sqlite3.connect(database.resolve().as_uri()+'?mode=ro',uri=True)
    connection.execute('PRAGMA query_only=ON')
    tables={name:[json.loads(row[0]) for row in connection.execute('SELECT record_json FROM '+name)]
            for name in ['sources','memories','objects','edges','requests']}
    check=connection.execute('PRAGMA quick_check').fetchone()[0]
    connection.close()
    nodes=[];memberships=[];active={};owners={}
    for owner in sorted({r['owner'] for r in tables['sources']}):
        source_rows=[s for s in tables['sources'] if s['owner']==owner]
        clocks=[s['message_at'] or s['created_at'] for s in source_rows]
        as_of=max(clocks,key=parse_time)
        memory_rows=[m for m in tables['memories'] if m['owner']==owner]
        for raw in memory_rows:
            view=visible_memory(Memory.model_validate(raw),as_of)
            if view and in_view(view.current,as_of) and (view.record_type == 'event' or view.current.mode == 'asserted'):active[(owner,view.id)]=view.current.id
        edges=[e for e in tables['edges'] if e['owner']==owner]
        owners[owner]={'as_of':as_of,'sources':len(source_rows),'memories':len(memory_rows), 'record_types':dict(Counter(m['record_type'] for m in memory_rows)),
            'revisions':sum(len(m['revisions']) for m in memory_rows),
            'entities':sum(e['owner']==owner for e in tables['objects']),
            'tags':dict(Counter(('event' if m['record_type'] == 'event' else 'fact') for m in memory_rows)),
            'relations':dict(Counter(e['relation'] for e in edges)),
            'active_relations':sum(active.get((owner,e['memory_id']))==e['revision_id'] and e['relation'] == 'mentions' for e in edges)}
    for name,node_type in [('sources','source'),('objects','entity'),('memories','memory')]:
        for raw in tables[name]:
            record=dict(raw)
            entity=node_type
            if node_type=='memory':
                revisions=record.pop('revisions')
                entity=record['record_type'];record['content']=Memory.model_validate(raw).current.content
                record['current_revision_id']=active.get((raw['owner'],raw['id']))
                for revision in revisions:
                    nodes.append({'id':raw['owner']+'/'+revision['id'],'entity':'revision','record':revision})
                    memberships.append({'origin':raw['owner']+'/'+raw['id'],'target':raw['owner']+'/'+revision['id'],
                                        'relation':'contains_revision','stored':False})
            nodes.append({'id':raw['owner']+'/'+raw['id'],'entity':entity,'record':record})
    links=[{**e,'origin':e['owner']+'/'+e['origin'],'target':e['owner']+'/'+e['target'],
            'active':active.get((e['owner'],e['memory_id']))==e['revision_id']} for e in tables['edges']]
    ids={node['id'] for node in nodes}
    dangling=[e['id'] for e in links if e['origin'] not in ids or e['target'] not in ids]
    data={'nodes':nodes,'links':links,'membership':memberships,'owners':owners,
          'summary':{'nodes':len(nodes),'stored_edges':len(links),'revision_memberships':len(memberships),
                     'dangling_edges':dangling,'quick_check':check,'mode':'sqlite_read_only'}}
    output.mkdir(parents=True)
    (output/'graph-data.json').write_text(json.dumps(data,ensure_ascii=False,indent=2))
    (output/'graph-summary.json').write_text(json.dumps({'owners':owners,**data['summary']},ensure_ascii=False,indent=2))
    return data['summary']


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('database',type=Path);parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    print(json.dumps(export(args.database,args.output)))


if __name__=='__main__':main()
