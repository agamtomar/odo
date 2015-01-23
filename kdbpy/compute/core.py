"""
A blaze backend that generates Q code
"""

from __future__ import absolute_import, print_function, division

import numbers
import datetime

from operator import attrgetter

import pandas as pd

from toolz.compatibility import zip
from toolz import map, first, second, compose

from into import resource, convert, into
from blaze import compute

from blaze.dispatch import dispatch

from blaze.compute.core import compute, swap_resources_into_scope
from blaze.expr import Symbol, Projection, Selection, Field
from blaze.expr import BinOp, UnaryOp, Expr, Reduction, By, Join, Head, Sort
from blaze.expr import Slice, Distinct, Summary, nelements
from blaze.expr import DateTime, Millisecond, Microsecond
from blaze.expr.datetime import Minute

from .. import q
from .qtable import QTable
from ..kdb import KQ
from ..util import parse_connection_string


qdatetimes = {
    'day': 'dd',
    'month': 'mm',
    'hour': 'hh',
    'second': 'ss',
}


def is_compute_symbol(x):
    return isinstance(x, q.List) and len(x) == 1 and isinstance(first(x),
                                                                q.Symbol)


def get(x):
    """Get a q atom from a single element list or return the list.

    Parameters
    ----------
    x : q.Expr
        A Q expression

    Returns
    -------
    r: q.Expr

    Examples
    --------
    >>> s = q.List(q.Atom('='), q.Symbol('t.name'), q.Symbol('Alice'))
    >>> s
    (=; `t.name; `Alice)
    >>> get(s)
    (=; `t.name; `Alice)
    >>> s = q.List(q.Symbol('t.name'))
    >>> get(s)
    `t.name
    """
    if len(x) == 1:
        return x[0]
    return x


def desubs(expr, t):
    """Remove a particular table `t` from an expression.

    TODO
    ----
    Is looking at the name of the table sufficient?

    Examples
    --------
    >>> import blaze as bz
    >>> s = q.Symbol('t.name')
    >>> desubs(s, 't')
    `name
    >>> s = q.List(q.Atom('first'), q.Symbol('t.name'))
    >>> s
    (first; `t.name)
    >>> desubs(s, 't')
    (first; `name)
    """
    # ignore the question mark needed for select, that's why we use *args[1:]
    result_type = {q.select: lambda *args: q.select(*args[1:])}
    result = list(_desubs(expr, t))
    return get(result_type.get(type(expr), q.List)(*result))


def compute_atom(atom, symbol):
    s = getattr(atom, 'str', atom.s)
    split = s.split('.', 1)
    if '.' in s and first(split) == getattr(symbol, '_name', symbol):
        return type(atom)(second(split))
    return atom


def _desubs(expr, t):
    if is_compute_symbol(expr):
        yield q.List(compute_atom(first(expr), t))
    elif isinstance(expr, q.Atom):
        yield compute_atom(expr, t)
    elif isinstance(expr, (basestring, numbers.Number, q.Bool)):
        yield expr
    else:
        for sube in expr:
            if isinstance(sube, q.Atom):
                yield compute_atom(sube, t)
            elif isinstance(sube, q.List):
                if is_compute_symbol(sube):
                    yield q.List(compute_atom(first(sube), t))
                else:
                    yield q.List(*(desubs(s, t) for s in sube))
            elif isinstance(sube, q.Dict):
                yield q.Dict([(desubs(k, t), desubs(v, t))
                              for k, v in sube.items()])
            else:  # isinstance(sube, (basestring, numbers.Number, q.Bool)):
                yield sube


@convert.register(q.Atom, (pd.Timestamp, datetime.datetime), cost=0.01)
def datetime_to_atom(d, **kwargs):
    # if we have a date only, do the proper q conversion
    if pd.Timestamp(d) == pd.Timestamp(d.date()):
        return into(q.Atom, d.date())
    return q.Atom(d.strftime('%Y.%m.%dD%H:%M:%S.%f000'))


@convert.register(q.Atom, datetime.date, cost=0.01)
def date_to_atom(d, **kwargs):
    return q.Atom(d.strftime('%Y.%m.%d'))


@dispatch(Projection, q.Expr)
def compute_up(expr, data, **kwargs):
    fields = list(map(q.Symbol, expr.fields))
    return q.select(data, aggregates=q.Dict(list(zip(fields, fields))))


@dispatch(BinOp, q.Expr, q.Expr)
def compute_up(expr, lhs, rhs, **kwargs):
    op = q.binops[expr.symbol]
    return op(lhs, rhs)


def qify(x):
    """Deal with putting q symbols in the AST.

    Examples
    --------
    >>> from blaze import Symbol
    >>> s = Symbol('s', 'var * {amount: float64, name: string}')
    >>> expr = s.name == 'Alice'
    >>> result = qify(expr.rhs)
    >>> result
    (,:[`Alice])
    >>> qify(1)
    1
    >>> qify('2014-01-02')
    2014.01.02
    >>> qify(pd.Timestamp('2014-01-02'))
    2014.01.02
    """
    assert not isinstance(x, Expr), 'input cannot be a blaze expression'
    if isinstance(x, basestring):
        try:
            return into(q.Atom, pd.Timestamp(x))
        except ValueError:
            return q.List(q.Symbol(x))
    elif isinstance(x, (datetime.date, datetime.datetime)):
        return into(q.Atom, x)
    else:
        return x


@dispatch(BinOp, q.Expr)
def compute_up(expr, data, **kwargs):
    op = q.binops[expr.symbol]
    if isinstance(expr.lhs, Expr):
        lhs, rhs = data, qify(expr.rhs)
    else:
        lhs, rhs = qify(expr.lhs), data
    return op(lhs, rhs)


@dispatch(Reduction, q.Expr)
def compute_up(expr, data, **kwargs):
    if expr.axis != (0,):
        raise ValueError("Axis keyword argument on reductions not supported")
    return q.unops[expr.symbol](data)


@dispatch(UnaryOp, q.Expr)
def compute_up(expr, data, **kwargs):
    return q.unops[expr.symbol](data)


@dispatch(Field, q.Expr)
def compute_up(expr, data, **kwargs):
    sym = q.Symbol(expr._name)

    try:
        return data[expr._name]
    except TypeError:
        # this is actually an exec call in q
        return q.select(data, grouper=q.List(), aggregates=q.List(sym))


@dispatch(Selection, q.Expr)
def compute_up(expr, data, **kwargs):
    # template: ?[table, predicate or list of predicates, by, aggregations]
    predicate = compute(expr.predicate, {expr._child: data})
    result = q.select(data, constraints=q.List(q.List(q.List(predicate))))
    leaf_name = expr._leaves()[0]._name
    return desubs(result, leaf_name)


@dispatch(DateTime, q.Expr)
def compute_up(expr, data, **kwargs):
    attr = expr.attr
    attr = qdatetimes.get(attr, attr)
    return data[attr]


@dispatch(Microsecond, q.Expr)
def compute_up(expr, data, **kwargs):
    return q.floor(q.div(q.mod(q.long(data), 1000000000), 1000))


@dispatch(Millisecond, q.Expr)
def compute_up(expr, data, **kwargs):
    return compute(expr._child.microsecond // 1000, data)


@dispatch(Minute, q.Expr)
def compute_up(expr, data, **kwargs):
    # q has mm for time types and mm for datetime and date types, this makes -1
    # amount of sense, so we bypass that and compute it our damn selves using
    # (`long$expr.minute) mod 60
    return q.mod(q.long(data[expr.attr]), 60)


@dispatch(Join, q.Expr, q.Expr)
def compute_up(expr, lhs, rhs, **kwargs):
    if expr.how != 'inner':
        raise NotImplementedError('only inner joins supported')
    if expr._on_left != expr._on_right:
        raise NotImplementedError('can only join on same named columns')
    return q.List('ej', q.symlist(expr._on_left), lhs, rhs)


@dispatch(Sort, q.Expr)
def compute_up(expr, data, **kwargs):
    return q.sort(data, expr._key, expr.ascending)


@dispatch(Summary, q.Expr)
def compute_up(expr, data, **kwargs):
    ops = [compute(op, {expr._child: data}) for op in expr.values]
    aggregates = q.Dict(list(zip(map(q.Symbol, expr.names), ops)))
    return desubs(q.select(data, aggregates=aggregates), expr._leaves()[0])


@dispatch(By, q.Expr)
def compute_up(expr, data, **kwargs):
    if isinstance(data, q.select):  # we are combining multiple selects
        child = data.child
        constraints = data.constraints
    else:
        child = data
        constraints = q.List()

    if not isinstance(expr.grouper, (Projection, Field, DateTime)):
        raise NotImplementedError('Grouping only allowed on Projection, Field '
                                  'and DateTime expressions')
    grouper = compute(expr.grouper, child)

    if hasattr(grouper, 'aggregates'):  # we have multiple grouping keys
        grouper = grouper.aggregates
    else:
        grouper = q.Dict([(q.Symbol(expr.grouper._name), grouper)])
    aggregates = compute(expr.apply, child).aggregates
    select = q.select(child, q.List(constraints), grouper, aggregates)
    return desubs(select, child.s)


@dispatch(nelements, q.Expr)
def compute_down(expr, data, **kwargs):
    if expr.axis != (0,):
        raise ValueError("axis == 1 not supported on record types")

    # if we have single field access on a table, that's the same as just
    # counting q's magic i variable
    if getattr(data, 'fields', ()) and not isinstance(data, q.select):
        # i is a magic variable in q indicating the row number
        return q.count(q.Symbol('i'))
    return q.count(data)


@dispatch(Head, q.Expr)
def compute_up(expr, data, **kwargs):
    return compute_up(expr._child[:expr.n], data, **kwargs)


@dispatch(Slice, q.Expr)
def compute_up(expr, data, **kwargs):
    assert len(expr.index) == 1, 'only single slice allowed'
    index, = expr.index

    # slicing a single row/element
    if isinstance(index, numbers.Integral):
        return q.slice1(data, int(index))

    rowcount = compute(expr._child.nrows, data)
    start = getattr(index, 'start', 0) or 0
    stop = getattr(index, 'stop', rowcount) or rowcount
    return q.slice(data, start, stop)


@dispatch(Distinct, q.Expr)
def compute_up(expr, data, **kwargs):
    return q.distinct(data)


@dispatch(Expr, QTable, QTable)
def compute_down(expr, lhs, rhs, **kwargs):
    # TODO: this is an anti-pattern
    # we should probably evaluate on the Q database
    lhs_leaf = expr._leaves()[0]
    rhs_leaf = expr._leaves()[1]
    new_lhs_leaf = Symbol(lhs.tablename, lhs_leaf.dshape)
    new_rhs_leaf = Symbol(rhs.tablename, rhs_leaf.dshape)
    new_expr = expr._subs({lhs_leaf: new_lhs_leaf, rhs_leaf: new_rhs_leaf})
    scope = {new_lhs_leaf: lhs._qsymbol, new_rhs_leaf: rhs._qsymbol}
    result_expr = compute(new_expr, scope)  # Return q.Expr, not data
    result = lhs.eval(result_expr)
    return result


@dispatch(Field, QTable)
def compute_down(expr, data, **kwargs):
    leaf = expr._leaves()[0]
    new_leaf = Symbol(data.tablename, leaf.dshape)
    new_expr = expr._subs({leaf: new_leaf})
    data_leaf = data._qsymbol

    if data_leaf.is_partitioned or data_leaf.is_splayed:
        result_expr = compute(new_expr._child[[new_expr._name]],
                              {new_leaf: data_leaf})
    else:
        # Return q.Expr, not data
        result_expr = compute(new_expr, {new_leaf: data_leaf})

    result = data.eval(result_expr).squeeze()
    result.name = expr._name
    return result


@dispatch(Expr, QTable)
def compute_down(expr, data, **kwargs):
    leaf = expr._leaves()[0]
    new_leaf = Symbol(data.tablename, leaf.dshape)
    new_expr = expr._subs({leaf: new_leaf})
    data_leaf = data._qsymbol

    result_expr = compute(new_expr,
                          {new_leaf: data_leaf})  # Return q.Expr, not data
    result = data.eval(result_expr)
    if isinstance(result, pd.Series):
        result.name = expr._name
        return result.reset_index(drop=True)
    elif isinstance(result, pd.DataFrame):
        # drop our index if all of our index's names are None
        return result.reset_index(drop=all(name is None
                                           for name in result.index.names))
    return result


def compile(data):
    expr, data = swap_resources_into_scope(data, data._resources())
    leaves = expr._leaves()
    import ipdb; ipdb.set_trace()
    data_leaves = map(compose(attrgetter('_qsymbol'), data.__getitem__),
                      leaves)
    return compute(expr, dict(zip(leaves, data_leaves)))


@dispatch(Field, KQ)
def compute_up(expr, data, **kwargs):
    return QTable(tablename=expr._name, engine=data)


@resource.register('kdb://.+', priority=13)
def resource_kdb(uri, engine=None, **kwargs):
    if engine is None:
        engine = KQ(parse_connection_string(uri), start=True)
    return engine
