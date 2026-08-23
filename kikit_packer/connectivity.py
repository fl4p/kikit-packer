def connected_components(instance_ids: list[str], edges: list[list[str]]) -> list[list[str]]:
    remaining = set(instance_ids)
    adjacency = {item: set() for item in instance_ids}
    for left, right in edges:
        adjacency[left].add(right)
        adjacency[right].add(left)
    output = []
    while remaining:
        start = min(remaining)
        pending = [start]
        component = set()
        while pending:
            item = pending.pop()
            if item in component:
                continue
            component.add(item)
            pending.extend(adjacency[item] - component)
        remaining -= component
        output.append(sorted(component))
    return output
