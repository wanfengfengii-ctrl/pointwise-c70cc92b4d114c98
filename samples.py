"""Small finite models and a complete, reproducible legacy SQLite catalog."""
from catalog import Catalog
from classifier import Classifier


def retry_model(silent='1/2'):
    return {
        'states': ['collector'], 'alphabet': ['x'],
        'initial': ['1'], 'final': ['1'],
        'edges': [
            {'id': 'retry', 'src': 'collector', 'dst': 'collector',
             'symbol': None, 'weight': silent},
            {'id': 'read', 'src': 'collector', 'dst': 'collector',
             'symbol': 'x', 'weight': '1/3'},
        ],
    }


def plain_model():
    model = retry_model('0')
    model['edges'] = model['edges'][1:]
    return model


def cycle_model():
    return {
        'states': ['capture', 'retry', 'accepted', 'unused'],
        'alphabet': ['x', 'y'],
        'initial': ['1', '0', '0', '0'],
        'final': ['0', '1/2', '1', '0'],
        'edges': [
            {'id': 'out', 'src': 'capture', 'dst': 'retry', 'symbol': None, 'weight': '1/2'},
            {'id': 'back', 'src': 'retry', 'dst': 'capture', 'symbol': None, 'weight': '1/3'},
            {'id': 'x1', 'src': 'retry', 'dst': 'accepted', 'symbol': 'x', 'weight': '2/3'},
            {'id': 'x2', 'src': 'capture', 'dst': 'accepted', 'symbol': 'x', 'weight': '1/4'},
            {'id': 'after', 'src': 'accepted', 'dst': 'retry', 'symbol': None, 'weight': '1/4'},
            {'id': 'repeat', 'src': 'accepted', 'dst': 'accepted', 'symbol': 'y', 'weight': '1/2'},
            {'id': 'zero', 'src': 'capture', 'dst': 'unused', 'symbol': None, 'weight': '0'},
        ],
    }


def explicit_model():
    return {
        'states': ['pending', 'done'], 'alphabet': ['x'],
        'initial': ['1', '0'], 'final': ['1', '1'],
        'edges': [
            {'id': 'stay', 'src': 'pending', 'dst': 'pending', 'symbol': 'x', 'weight': '1/2'},
            {'id': 'finish', 'src': 'pending', 'dst': 'done', 'symbol': 'x', 'weight': '1/2'},
            {'id': 'tail', 'src': 'done', 'dst': 'done', 'symbol': 'x', 'weight': '1'},
        ],
    }


def populate(catalog):
    """Seed only an empty catalog, atomically; include old and new fixed tasks."""
    with catalog.atomic():
        for table in ('model_revisions', 'tasks', 'classification_runs'):
            if catalog.db.execute('SELECT 1 FROM ' + table + ' LIMIT 1').fetchone():
                raise ValueError('catalog must be empty')
        catalog.save_model('retrying', retry_model())
        catalog.save_model('plain', plain_model())
        catalog.create_task('frozen', {
            'retry': ('retrying', None, 1), 'plain': ('plain', None, 2),
        })
        catalog.save_model('retrying', retry_model('1/4'))
        catalog.create_task('current', {
            'retry': ('retrying', None, 1), 'plain': ('plain', None, 2),
        })
        catalog.save_model('cycle', cycle_model())
        Classifier(catalog).classify('frozen', ())
        Classifier(catalog).classify('frozen', ('x',))
        Classifier(catalog).classify('current', ())
    return catalog


def build_legacy(path):
    """Build a real v1 database and close it; refuses a populated catalog."""
    with Catalog(path) as catalog:
        populate(catalog)
