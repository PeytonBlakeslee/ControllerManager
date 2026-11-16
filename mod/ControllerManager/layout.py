"""Pure controller layouts, including deliberately unbound action bundles."""
KEYS = tuple('XboxTypeS_' + key for key in (
    'A','B','X','Y','LeftShoulder','RightShoulder','LeftTrigger','RightTrigger',
    'LeftThumbstick','RightThumbstick','DPad_Up','DPad_Down','DPad_Left','DPad_Right'))
SHORT = dict(zip(KEYS, ('A','B','X','Y','LB','RB','LT','RT','LS','RS','D-pad Up','D-pad Down','D-pad Left','D-pad Right')))
FROM_SHORT = {value:key for key,value in SHORT.items()}
_CANONICAL = {key.casefold():key for key in KEYS}


def canonical(key):
    """Unreal FNames are case-insensitive; settings identifiers stay stable."""
    text = str(key)
    return _CANONICAL.get(text.casefold(),text)


def resolve_keys(values):
    """Resolve logical buttons to actual keys, preserving their spelling/type."""
    result = {}
    for value in values:
        key = canonical(value)
        if key not in KEYS:
            continue
        if key in result:
            raise ValueError(f'ambiguous runtime button {key}: {result[key]!r}, {value!r}')
        result[key] = value
    return result,tuple(key for key in KEYS if key not in result)



def validate(mapping):
    if set(mapping)!=set(KEYS): raise ValueError('Incomplete action layout')
    assigned=[v for v in mapping.values() if v is not None]
    if any(v not in KEYS for v in assigned) or len(assigned)!=len(set(assigned)):
        raise ValueError('Invalid or duplicate controller assignment')
    return mapping


def assign(mapping,source,target,swap=True):
    validate(mapping)
    if source not in KEYS or target is not None and target not in KEYS:
        raise ValueError('Reserved or unsupported controller button')
    result = dict(mapping)
    previous = result[source]
    occupant = next((key for key in KEYS if target is not None and result[key] == target),None)
    if occupant is not None and occupant!=source:
        result[occupant] = previous if swap else None
    result[source] = target
    return validate(result)


def solve(baseline,choices):
    result = dict(validate(baseline))
    for key in KEYS:
        if choices.get(key)=='Unbound': result[key]=None
    for key in KEYS:
        value = choices.get(key,'Default')
        if value not in ('Default','Unbound'):
            if value not in FROM_SHORT:
                raise ValueError('unknown controller button')
            result = assign(result,key,FROM_SHORT[value])
    return validate(result)


def encode(mapping,baseline):
    validate(mapping)
    return {key:('Unbound' if mapping[key] is None else 'Default' if mapping[key]==baseline[key] else SHORT[mapping[key]]) for key in KEYS}


# JSON-safe queued user preferences, applied against the reflected native layout.
import copy

VALUES=('Default','Unbound',*SHORT.values())
CONTEXTS=('foot','vehicle')


def state(value):
    if not isinstance(value,dict): raise ValueError('Invalid saved layout metadata')
    result=copy.deepcopy(value)
    cache=result.get('baseline',{})
    if cache:
        validate(cache)
        if any(v is None for v in cache.values()): raise ValueError('Cached native baseline is incomplete')
    pending=result.setdefault('pending',[])
    if not isinstance(pending,list) or len(pending)>512: raise ValueError('Invalid pending binding edits')
    for item in pending:
        if not isinstance(item,dict) or item.get('context') not in CONTEXTS:
            raise ValueError('Invalid pending context')
        if item.get('action')=='reset': continue
        if item.get('action') not in KEYS or item.get('value') not in VALUES or not isinstance(item.get('swap'),bool):
            raise ValueError('Invalid pending binding edit')
    return result


def edit(mapping,baseline,action,value,swap):
    if action=='reset': return dict(baseline)
    if value not in VALUES: raise ValueError('Invalid saved controller button')
    target=baseline[action] if value=='Default' else None if value=='Unbound' else FROM_SHORT[value]
    return assign(mapping,action,target,swap)



def canonical_choices(choices,mapping,baseline):
    """Retain explicit button intent; only Default inherits a future preset edit."""
    encoded=encode(mapping,baseline)
    for key in KEYS:
        value=choices.get(key,'Default')
        if value in FROM_SHORT and FROM_SHORT[value]==mapping[key]:encoded[key]=value
    return encoded


def apply_edit(choices,baseline,context,action,value,swap):
    result={c:dict(v) for c,v in choices.items()}
    old=solve(baseline,choices[context])
    updated=edit(old,baseline,action,value,swap)
    result[context]=canonical_choices(choices[context],updated,baseline)
    if action=='reset':result[context]={k:'Default' for k in KEYS}
    else:
        for key in KEYS:
            if key!=action and updated[key]!=old[key]:
                result[context][key]='Unbound' if updated[key] is None else SHORT[updated[key]]
        result[context][action]=value
    return result


def finalize(choices,metadata,baseline):
    saved=state(metadata)
    result={c:canonical_choices(choices[c],solve(baseline,choices[c]),baseline) for c in CONTEXTS}
    for item in saved['pending']:
        result=apply_edit(result,baseline,item['context'],item['action'],item.get('value','Default'),item.get('swap',True))
    saved.update(version=3,baseline=dict(baseline),pending=[])
    return result,saved


def queue(metadata,context,action,value,swap):
    saved=state(metadata)
    if len(saved['pending'])>=512: raise ValueError('Load a character to apply the queued edits before adding more')
    saved['pending'].append(dict(context=context,action=action,value=value,swap=bool(swap)))
    return state(saved)
