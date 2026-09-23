"""THROWAWAY: identical synthetic graph in scratch SQLite, Python and Neo4j.

No application imports, profiles, providers, or production data. Runs only in
the dedicated Compose project. Result comparisons are experimental checks,
not a production regression suite. All fixture edges are recorded facts;
planning eligibility is evaluated separately in the lifecycle experiment.
"""
from collections import defaultdict
from itertools import combinations
import hashlib
import http.client
import json
import os
import platform
import sqlite3
from statistics import median
import time
from urllib.parse import urlparse

SQL = {
    "dependents": """WITH RECURSIVE affected(id) AS (
        SELECT src FROM edge WHERE kind='DEPENDS_ON' AND dst=:id
        UNION SELECT e.src FROM edge e JOIN affected a ON e.dst=a.id
        WHERE e.kind='DEPENDS_ON') SELECT id FROM affected ORDER BY id""",
    "requirement_impact": """WITH RECURSIVE affected(id) AS (
        SELECT dst FROM edge WHERE src=:id AND kind='DERIVES'
        UNION SELECT e.dst FROM edge e JOIN affected a ON e.src=a.id
        WHERE e.kind IN ('HAS_TASK','TARGETS')) SELECT id FROM affected ORDER BY id""",
    "blocking_paths": """WITH RECURSIVE walk(id,path,depth) AS (
        SELECT :id, '|'||:id||'|', 0
        UNION ALL SELECT e.dst,w.path||e.dst||'|',w.depth+1
        FROM walk w JOIN edge e ON e.src=w.id
        WHERE e.kind='DEPENDS_ON' AND w.depth<32
        AND instr(w.path,'|'||e.dst||'|')=0)
        SELECT path FROM walk WHERE id=:target AND depth>0 ORDER BY path""",
    "shared_files": """SELECT a.src,b.src,a.dst FROM edge a JOIN edge b
        ON a.dst=b.dst AND a.src<b.src
        WHERE a.kind='TARGETS' AND b.kind='TARGETS' ORDER BY a.src,b.src,a.dst""",
    "obsolete_endpoints": """SELECT e.src,e.dst FROM edge e JOIN node n ON n.id=e.dst
        WHERE e.kind='DEPENDS_ON' AND n.state='superseded' ORDER BY e.src,e.dst""",
}
CYPHER = {
    "dependents": """MATCH (s:Entity {id:$id})<-[:DEPENDS_ON*1..32]-(d)
        RETURN DISTINCT d.id ORDER BY d.id""",
    "requirement_impact": """MATCH (:Entity {id:$id})-[:DERIVES]->(s)
        MATCH (s)-[:HAS_TASK|TARGETS*0..2]->(affected)
        RETURN DISTINCT affected.id ORDER BY affected.id""",
    "blocking_paths": """MATCH p=(:Entity {id:$id})-[:DEPENDS_ON*1..32]->(:Entity {id:$target})
        WHERE all(n IN nodes(p) WHERE single(m IN nodes(p) WHERE m=n))
        RETURN reduce(text='|', n IN nodes(p) | text+n.id+'|') AS path ORDER BY path""",
    "shared_files": """MATCH (a)-[:TARGETS]->(f)<-[:TARGETS]-(b)
        WHERE a.id<b.id RETURN a.id,b.id,f.id ORDER BY a.id,b.id,f.id""",
    "obsolete_endpoints": """MATCH (b)-[:DEPENDS_ON]->(a:Entity {state:'superseded'})
        RETURN b.id,a.id ORDER BY b.id,a.id""",
}


def graph_request(statement, parameters=None):
    target = urlparse(os.environ.get('NEO4J_URL', 'http://graph:7474'))
    connection = http.client.HTTPConnection(target.hostname, target.port, timeout=90)
    body = json.dumps({'statement': statement, 'parameters': parameters or {}, 'maxExecutionTime': 60})
    connection.request('POST', '/db/neo4j/query/v2', body,
                       {'Content-Type': 'application/json', 'Accept': 'application/json'})
    response = connection.getresponse()
    payload = json.loads(response.read())
    connection.close()
    if response.status != 202 or payload.get('errors'):
        raise RuntimeError(payload)
    return payload.get('data', {}).get('values', [])


def fixture(size):
    nodes = {}
    edges = []
    def add(identity, kind, state='current'):
        nodes[identity] = {'id': identity, 'kind': kind, 'state': state}
    def link(src, kind, dst):
        edges.append({'src': src, 'kind': kind, 'dst': dst})
    for i in range(size):
        group, offset = divmod(i, 12)
        story, task, requirement, file = f'S{i:05}', f'T{i:05}', f'R{group:05}', f'F{i//3:05}'
        add(story, 'Story', 'superseded' if offset == 0 and group % 4 == 0 else 'current')
        add(task, 'Task')
        add(requirement, 'Requirement')
        add(file, 'File')
        link(requirement, 'DERIVES', story)
        link(story, 'HAS_TASK', task)
        link(task, 'TARGETS', file)
        if offset:
            link(story, 'DEPENDS_ON', f'S{group*12+(offset-1)//2:05}')
        elif group:
            # Connect every group through a binary tree of group roots. Maximum
            # fixture depth stays below 32, so Cypher's explicit bound is complete.
            link(story, 'DEPENDS_ON', f'S{((group-1)//2)*12:05}')
        if offset == 11:
            link(story, 'DEPENDS_ON', f'S{group*12+3:05}')
        if offset == 0 and group % 4 == 0:
            replacement = f'C{i:05}'
            add(replacement, 'Story')
            link(replacement, 'SUPERSEDES', story)
            link(requirement, 'DERIVES', replacement)
    return list(nodes.values()), edges


def sqlite_load(nodes, edges):
    db = sqlite3.connect('/tmp/PROTOTYPE-wipe-me.sqlite3')
    db.executescript('''PRAGMA foreign_keys=ON;
        DROP TABLE IF EXISTS edge; DROP TABLE IF EXISTS node;
        CREATE TABLE node(id TEXT PRIMARY KEY,kind TEXT NOT NULL,state TEXT NOT NULL);
        CREATE TABLE edge(src TEXT REFERENCES node(id),kind TEXT NOT NULL,
            dst TEXT REFERENCES node(id),PRIMARY KEY(src,kind,dst));
        CREATE INDEX edge_dst_kind_src ON edge(dst,kind,src);
        CREATE INDEX node_state ON node(state);''')
    db.executemany('INSERT INTO node VALUES (:id,:kind,:state)', nodes)
    db.executemany('INSERT INTO edge VALUES (:src,:kind,:dst)', edges)
    db.commit()
    return db


def graph_load(nodes, edges):
    graph_request('MATCH (n:Entity) DETACH DELETE n')
    graph_request('CREATE CONSTRAINT entity_id IF NOT EXISTS FOR (n:Entity) REQUIRE n.id IS UNIQUE')
    graph_request('CREATE INDEX entity_state IF NOT EXISTS FOR (n:Entity) ON (n.state)')
    for start in range(0, len(nodes), 2000):
        graph_request('UNWIND $rows AS row CREATE (n:Entity) SET n=row', {'rows': nodes[start:start+2000]})
    for kind in sorted({e['kind'] for e in edges}):
        group = [e for e in edges if e['kind'] == kind]
        for start in range(0, len(group), 2000):
            # Relation names come only from the closed, internal fixture vocabulary.
            graph_request(f'''UNWIND $rows AS row MATCH (a:Entity {{id:row.src}}), (b:Entity {{id:row.dst}})
                CREATE (a)-[:{kind}]->(b)''', {'rows': group[start:start+2000]})
    graph_request('CALL db.awaitIndexes(60)')


def projection(nodes, edges):
    out, incoming = defaultdict(list), defaultdict(list)
    for edge in edges:
        out[edge['src']].append((edge['kind'], edge['dst']))
        incoming[edge['dst']].append((edge['kind'], edge['src']))
    states = {n['id']: n['state'] for n in nodes}
    def reach(start, adjacency, kinds):
        todo = [start]
        seen = {start}
        while todo:
            for kind, node in adjacency[todo.pop()]:
                if kind in kinds and node not in seen:
                    seen.add(node)
                    todo.append(node)
        return [[n] for n in sorted(seen - {start})]
    def query(name, params):
        if name == 'dependents':
            return reach(params['id'], incoming, {'DEPENDS_ON'})
        if name == 'requirement_impact':
            return reach(params['id'], out, {'DERIVES','HAS_TASK','TARGETS'})
        if name == 'blocking_paths':
            paths = []
            def walk(path):
                if path[-1] == params['target']:
                    paths.append(['|'+'|'.join(path)+'|'])
                elif len(path) <= 32:
                    for kind, node in out[path[-1]]:
                        if kind == 'DEPENDS_ON' and node not in path:
                            walk(path+[node])
            walk([params['id']])
            return sorted(paths)
        if name == 'shared_files':
            return sorted([a,b,file] for file, parents in incoming.items()
                          for a,b in combinations(sorted(n for kind,n in parents if kind=='TARGETS'),2))
        return sorted([e['src'],e['dst']] for e in edges
                      if e['kind']=='DEPENDS_ON' and states[e['dst']]=='superseded')
    return query


def measured(call):
    call()  # one untimed warm-up, including query-plan compilation
    samples = []
    for _ in range(7):
        start = time.perf_counter()
        result = call()
        samples.append((time.perf_counter()-start)*1000)
    return result, round(median(samples), 4), [round(s,4) for s in samples]


def lifecycle():
    nodes = [{'id':n,'kind':'Story','state':'current'} for n in 'AB']
    edges = [{'src':'B','kind':'DEPENDS_ON','dst':'A'}]
    db = sqlite_load(nodes, edges)
    graph_load(nodes, edges)
    review = {'selected':['B'],'approved_edges':[['B','A']], 'source_fingerprint':'selected-B-v1'}
    pinned = json.loads(json.dumps(review))
    with db:
        db.execute("UPDATE node SET state='superseded' WHERE id='A'")
        db.execute("INSERT INTO node VALUES ('C','Story','current')")
        db.execute("INSERT INTO edge VALUES ('C','SUPERSEDES','A')")
    graph_request("MATCH (a:Entity {id:'A'}) SET a.state='superseded' CREATE (c:Entity {id:'C',kind:'Story',state:'current'}) CREATE (c)-[:SUPERSEDES]->(a)")
    sql_stale = [list(r) for r in db.execute(SQL['obsolete_endpoints'])]
    graph_stale = graph_request(CYPHER['obsolete_endpoints'])
    assert sql_stale == graph_stale == [['B','A']]
    unchanged = {'sqlite_stale_edges':sql_stale,'neo4j_stale_edges':graph_stale,
                 'illustrative_review':review.copy(),'illustrative_review_unchanged':review==pinned,
                 'migration_alone_fixes_188':False}
    # Same explicit application policy in both experiments. This is the missing
    # rule, not a database-native automatic action. There is no active Sprint here.
    review_required = bool(sql_stale)
    assert review_required
    history = json.loads(json.dumps(review))
    with db:
        db.execute("DELETE FROM edge WHERE src='B' AND kind='DEPENDS_ON' AND dst='A'")
        db.execute("INSERT INTO edge VALUES ('B','DEPENDS_ON','C')")
    graph_request("MATCH (:Entity {id:'B'})-[r:DEPENDS_ON]->(:Entity {id:'A'}) DELETE r WITH count(*) AS removed MATCH (b:Entity {id:'B'}),(c:Entity {id:'C'}) CREATE (b)-[:DEPENDS_ON]->(c)")
    sql_after = [list(r) for r in db.execute("SELECT src,dst FROM edge WHERE kind='DEPENDS_ON'")]
    graph_after = graph_request('MATCH (b)-[:DEPENDS_ON]->(a) RETURN b.id,a.id')
    assert sql_after == graph_after == [['B','C']]
    assert pinned == history
    db.close()
    return {'storage_only':unchanged, 'explicit_policy':{
        'assumption':'Operator approves B depending on C; the probe does not execute or persist a human review.',
        'review_required_before_repair':review_required, 'sqlite_edges_after_assumed_approval':sql_after,
        'neo4j_edges_after_assumed_approval':graph_after,'historical_review_in_memory':history,
        'pinned_snapshot_unchanged':True,
        'limitation':'History is copied in memory for this probe; production transactions, Sprint locks, and audit persistence are not implemented.'}}


def main():
    wait_started = time.monotonic()
    for attempt in range(90):
        try:
            graph_request('RETURN 1')
            break
        except (OSError, ValueError, RuntimeError):
            if attempt == 89:
                raise
            time.sleep(1)
    result = {'status':'completed','generated_at':time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime()),
              'engine_versions':{'python':platform.python_version(),'sqlite':sqlite3.sqlite_version,
                  'neo4j':graph_request('CALL dbms.components() YIELD name,versions,edition RETURN name,versions,edition'),
                  'platform':platform.platform()},
              'readiness_wait_seconds':round(time.monotonic()-wait_started,3),
              'protocol':'One warm-up + seven measured iterations per query; warm medians. Neo4j HTTP client time includes serialization, new connection, execution, transfer and decoding. SQLite in-process. Projection rebuild measured separately. No cold-start or general throughput conclusion.',
              'datasets':[], 'query_sources':{'sqlite':SQL,'neo4j':CYPHER}}
    for size in (48,480,4800):
        nodes, edges = fixture(size)
        start = time.perf_counter()
        db = sqlite_load(nodes,edges)
        sql_load = (time.perf_counter()-start)*1000
        start = time.perf_counter()
        graph_load(nodes,edges)
        neo_load = (time.perf_counter()-start)*1000
        # Include reading both tables and constructing adjacency in rebuild cost.
        start = time.perf_counter()
        loaded_nodes = [dict(zip(('id','kind','state'),row)) for row in db.execute('SELECT * FROM node')]
        loaded_edges = [dict(zip(('src','kind','dst'),row)) for row in db.execute('SELECT * FROM edge')]
        query_projection = projection(loaded_nodes,loaded_edges)
        rebuild = (time.perf_counter()-start)*1000
        dataset = {'label':f'{size} Stories','nodes':len(nodes),'edges':len(edges),
                   'fixture_sha256':hashlib.sha256(json.dumps([nodes,edges],sort_keys=True).encode()).hexdigest(),
                   'sqlite_load_ms':round(sql_load,3),'neo4j_load_ms':round(neo_load,3),
                   'projection_rebuild_ms':round(rebuild,3),'queries':[]}
        for name in SQL:
            params = {'id':'R00000' if name=='requirement_impact' else 'S00011' if name=='blocking_paths' else 'S00000', 'target':'S00000'}
            sql_rows,sql_ms,sql_samples = measured(lambda: [list(r) for r in db.execute(SQL[name],params)])
            py_rows,py_ms,py_samples = measured(lambda: query_projection(name,params))
            neo_rows,neo_ms,neo_samples = measured(lambda: graph_request(CYPHER[name],params))
            assert sql_rows == py_rows == neo_rows, (name,sql_rows[:10],py_rows[:10],neo_rows[:10])
            dataset['queries'].append({'name':name,'sqlite_ms':sql_ms,'projection_ms':py_ms,'neo4j_ms':neo_ms,
                'result_count':len(sql_rows),'equal':True,'sample_rows':sql_rows[:5],
                'result_sha256':hashlib.sha256(json.dumps(sql_rows).encode()).hexdigest(),
                'samples_ms':{'sqlite':sql_samples,'projection':py_samples,'neo4j':neo_samples}})
        db.close()
        result['datasets'].append(dataset)
    result['lifecycle'] = lifecycle()
    print(json.dumps(result,indent=2))


if __name__ == '__main__':
    main()
