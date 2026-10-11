"""Let FastAPI put synchronous planting work in its worker pool."""
import ast

def transform(source):
    tree=ast.parse(source);lines=source.splitlines(keepends=True)
    for node in tree.body:
        if isinstance(node,ast.AsyncFunctionDef) and node.name in ('analyze_zone','route_planting_sites'):
            if any(isinstance(child,(ast.Await,ast.AsyncFor,ast.AsyncWith)) for child in ast.walk(node)):
                continue
            line=lines[node.lineno-1]
            if 'async def ' not in line:raise ValueError('Could not identify planting route header.')
            lines[node.lineno-1]=line.replace('async def ','def ',1)
    result=''.join(lines);compile(result,'EcoTrace responsive planting routes','exec');return result
