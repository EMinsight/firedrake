import numpy
from functools import partial

import FIAT
import ufl

from coffee import base as ast
from pyop2 import op2

from tsfc.fiatinterface import create_element
from tsfc import compile_expression_at_points as compile_ufl_kernel

import firedrake
from firedrake import utils

__all__ = ("interpolate", "Interpolator", "Interpolationoperator")


def interpolate(expr, V, subset=None):
    """Interpolate an expression onto a new function in V.

    :arg expr: an :class:`.Expression`.
    :arg V: the :class:`.FunctionSpace` to interpolate into (or else
        an existing :class:`.Function`).
    :kwarg subset: An optional :class:`pyop2.Subset` to apply the
        interpolation over.

    Returns a new :class:`.Function` in the space ``V`` (or ``V`` if
    it was a Function).

    .. note::

       If you find interpolating the same expression again and again
       (for example in a time loop) you may find you get better
       performance by using a :class:`Interpolator` instead.
    """
    return Interpolator(expr, V, subset=subset).interpolate()


class Interpolator(object):
    """A reusable interpolation object.

    :arg expr: The expression to interpolate.
    :arg V: The :class:`.FunctionSpace` or :class:`.Function` to
        interpolate into.

    This object can be used to carry out the same interpolation
    multiple times (for example in a timestepping loop).

    .. note::

       The :class:`Interpolator` holds a reference to the provided
       arguments (such that they won't be collected until the
       :class:`Interpolator` is also collected).
    """
    def __init__(self, expr, V, subset=None):
        self.callable = make_interpolator(expr, V, subset)

    @utils.known_pyop2_safe
    def interpolate(self):
        """Compute the interpolation.

        :returns: The resulting interpolated :class:`.Function`.
        """
        return self.callable()


class SubExpression(object):
    """A helper class for interpolating onto mixed functions.

    Allows using the user arguments from a provided expression, but
    overrides the code to pull out.
    """
    def __init__(self, expr, idx, shape):
        self._expr = expr
        self.code = numpy.array(expr.code[idx]).flatten()
        self._shape = shape
        self.ufl_shape = shape

    def value_shape(self):
        return self._shape

    def rank(self):
        return len(self.ufl_shape)

    def __getattr__(self, name):
        return getattr(self._expr, name)


def make_interpolator(expr, V, subset):
    assert isinstance(expr, ufl.classes.Expr)

    if isinstance(V, firedrake.Function):
        f = V
        V = f.function_space()
    else:
        f = firedrake.Function(V)

    # Make sure we have an expression of the right length i.e. a value for
    # each component in the value shape of each function space
    dims = [numpy.prod(fs.ufl_element().value_shape(), dtype=int)
            for fs in V]
    loops = []
    if numpy.prod(expr.ufl_shape, dtype=int) != sum(dims):
        raise RuntimeError('Expression of length %d required, got length %d'
                           % (sum(dims), numpy.prod(expr.ufl_shape, dtype=int)))

    if not isinstance(expr, firedrake.Expression):
        if len(V) > 1:
            raise NotImplementedError(
                "UFL expressions for mixed functions are not yet supported.")
        loops.extend(_interpolator(V, f.dat, expr, subset))
    elif hasattr(expr, 'eval'):
        if len(V) > 1:
            raise NotImplementedError(
                "Python expressions for mixed functions are not yet supported.")
        loops.extend(_interpolator(V, f.dat, expr, subset))
    else:
        # Slice the expression and pass in the right number of values for
        # each component function space of this function
        d = 0
        for fs, dat, dim in zip(V, f.dat, dims):
            idx = d if fs.rank == 0 else slice(d, d+dim)
            loops.extend(_interpolator(fs, dat,
                                       SubExpression(expr, idx, fs.ufl_element().value_shape()),
                                       subset))
            d += dim

    def callable(loops, f):
        for l in loops:
            l()
        return f

    return partial(callable, loops, f)


def _interpolator(V, dat, expr, subset):
    to_element = create_element(V.ufl_element(), vector_is_mixed=False)
    to_pts = []

    if V.ufl_element().mapping() != "identity":
        raise NotImplementedError("Can only interpolate onto elements "
                                  "with affine mapping. Try projecting instead")

    for dual in to_element.dual_basis():
        if not isinstance(dual, FIAT.functional.PointEvaluation):
            raise NotImplementedError("Can only interpolate onto point "
                                      "evaluation operators. Try projecting instead")
        pts, = dual.pt_dict.keys()
        to_pts.append(pts)

    if len(expr.ufl_shape) != len(V.ufl_element().value_shape()):
        raise RuntimeError('Rank mismatch: Expression rank %d, FunctionSpace rank %d'
                           % (len(expr.ufl_shape), len(V.ufl_element().value_shape())))

    if expr.ufl_shape != V.ufl_element().value_shape():
        raise RuntimeError('Shape mismatch: Expression shape %r, FunctionSpace shape %r'
                           % (expr.ufl_shape, V.ufl_element().value_shape()))

    mesh = V.ufl_domain()
    coords = mesh.coordinates

    if not isinstance(expr, (firedrake.Expression, SubExpression)):
        if expr.ufl_domain() and expr.ufl_domain() != V.mesh():
            raise NotImplementedError("Interpolation onto another mesh not supported.")
        if expr.ufl_shape != V.shape:
            raise ValueError("UFL expression has incorrect shape for interpolation.")
        ast, oriented, coefficients = compile_ufl_kernel(expr, to_pts, coords)
        kernel = op2.Kernel(ast, ast.name)
        indexed = True
    elif hasattr(expr, "eval"):
        kernel, oriented, coefficients = compile_python_kernel(expr, to_pts, to_element, V, coords)
        indexed = False
    elif expr.code is not None:
        kernel, oriented, coefficients = compile_c_kernel(expr, to_pts, to_element, V, coords)
        indexed = True
    else:
        raise RuntimeError("Attempting to evaluate an Expression which has no value.")

    cell_set = coords.cell_set
    if subset is not None:
        assert subset.superset == cell_set
        cell_set = subset
    args = [kernel, cell_set]

    copy_back = False
    if dat in set((c.dat for c in coefficients)):
        output = dat
        dat = op2.Dat(dat.dataset)
        copy_back = True
    if indexed:
        args.append(dat(op2.WRITE, V.cell_node_map()[op2.i[0]]))
    else:
        args.append(dat(op2.WRITE, V.cell_node_map()))
    if oriented:
        co = mesh.cell_orientations()
        args.append(co.dat(op2.READ, co.cell_node_map()))
    for coefficient in coefficients:
        args.append(coefficient.dat(op2.READ, coefficient.cell_node_map()))

    if copy_back:
        return partial(op2.par_loop, *args), partial(dat.copy, output)
    else:
        return (partial(op2.par_loop, *args), )


class GlobalWrapper(object):
    """Wrapper object that fakes a Global to behave like a Function."""
    def __init__(self, glob):
        self.dat = glob
        self.cell_node_map = lambda *args: None


def compile_python_kernel(expression, to_pts, to_element, fs, coords):
    """Produce a :class:`PyOP2.Kernel` wrapping the eval method on the
    function provided."""

    coords_space = coords.function_space()
    coords_element = create_element(coords_space.ufl_element(), vector_is_mixed=False)

    X_remap = list(coords_element.tabulate(0, to_pts).values())[0]

    # The par_loop will just pass us arguments, since it doesn't
    # know about keyword args at all so unpack into a dict that we
    # can pass to the user's eval method.
    def kernel(output, x, *args):
        kwargs = {}
        for (slot, _), arg in zip(expression._user_args, args):
            kwargs[slot] = arg
        X = numpy.dot(X_remap.T, x)

        for i in range(len(output)):
            # Pass a slice for the scalar case but just the
            # current vector in the VFS case. This ensures the
            # eval method has a Dolfin compatible API.
            expression.eval(output[i:i+1, ...] if numpy.ndim(output) == 1 else output[i, ...],
                            X[i:i+1, ...] if numpy.ndim(X) == 1 else X[i, ...], **kwargs)

    coefficients = [coords]
    for _, arg in expression._user_args:
        coefficients.append(GlobalWrapper(arg))
    return kernel, False, tuple(coefficients)


def compile_c_kernel(expression, to_pts, to_element, fs, coords):
    """Produce a :class:`PyOP2.Kernel` from the c expression provided."""

    coords_space = coords.function_space()
    coords_element = create_element(coords_space.ufl_element(), vector_is_mixed=False)

    names = {v[0] for v in expression._user_args}

    X = list(coords_element.tabulate(0, to_pts).values())[0]

    # Produce C array notation of X.
    X_str = "{{"+"},\n{".join([",".join(map(str, x)) for x in X.T])+"}}"

    A = utils.unique_name("A", names)
    X = utils.unique_name("X", names)
    x_ = utils.unique_name("x_", names)
    k = utils.unique_name("k", names)
    d = utils.unique_name("d", names)
    i_ = utils.unique_name("i", names)
    # x is a reserved name.
    x = "x"
    if "x" in names:
        raise ValueError("cannot use 'x' as a user-defined Expression variable")
    ass_exp = [ast.Assign(ast.Symbol(A, (k,), ((len(expression.code), i),)),
                          ast.FlatBlock("%s" % code))
               for i, code in enumerate(expression.code)]

    dim = coords_space.value_size
    ndof = to_element.space_dimension()
    xndof = coords_element.space_dimension()
    nfdof = to_element.space_dimension() * numpy.prod(fs.value_size, dtype=int)

    init_X = ast.Decl(typ="double", sym=ast.Symbol(X, rank=(ndof, xndof)),
                      qualifiers=["const"], init=X_str)
    init_x = ast.Decl(typ="double", sym=ast.Symbol(x, rank=(coords_space.value_size,)))
    init_pi = ast.Decl(typ="double", sym="pi", qualifiers=["const"],
                       init="3.141592653589793")
    init = ast.Block([init_X, init_x, init_pi])
    incr_x = ast.Incr(ast.Symbol(x, rank=(d,)),
                      ast.Prod(ast.Symbol(X, rank=(k, i_)),
                               ast.Symbol(x_, rank=(i_, d))))
    assign_x = ast.Assign(ast.Symbol(x, rank=(d,)), 0)
    loop_x = ast.For(init=ast.Decl("unsigned int", i_, 0),
                     cond=ast.Less(i_, xndof),
                     incr=ast.Incr(i_, 1), body=[incr_x])

    block = ast.For(init=ast.Decl("unsigned int", d, 0),
                    cond=ast.Less(d, dim),
                    incr=ast.Incr(d, 1), body=[assign_x, loop_x])
    loop = ast.c_for(k, ndof,
                     ast.Block([block] + ass_exp, open_scope=True))
    user_args = []
    user_init = []
    for _, arg in expression._user_args:
        if arg.shape == (1, ):
            user_args.append(ast.Decl("double *", "%s_" % arg.name))
            user_init.append(ast.FlatBlock("const double %s = *%s_;" %
                                           (arg.name, arg.name)))
        else:
            user_args.append(ast.Decl("double *", arg.name))
    kernel_code = ast.FunDecl("void", "expression_kernel",
                              [ast.Decl("double", ast.Symbol(A, (nfdof,))),
                               ast.Decl("double**", x_)] + user_args,
                              ast.Block(user_init + [init, loop],
                                        open_scope=False))
    coefficients = [coords]
    for _, arg in expression._user_args:
        coefficients.append(GlobalWrapper(arg))
    return op2.Kernel(kernel_code, kernel_code.name), False, tuple(coefficients)


def Interpolationoperator(Vdonor, Vtarget):
  """
  Function to compute the Interpolation Matrix.

    :param Vdonor: the FunctionSpace from which to interpolate.
    :param Vtarget: the FunctionSpace to interpolate to.

  Returns the PETSc MAT object containing the Interpolationmatrix.
  """

  from firedrake.petsc import PETSc
  from mpi4py import MPI
  from .modified_pointeval_utils import compile_element, make_c_evaluate
  import ctypes
  from pyop2 import compilation
  from firedrake.function import _CFunction
  import ufl
  import firedrake

  # Initialise the mat PETSc operator
  mat = PETSc.Mat().create(comm=Vdonor.comm)
  mat.setSizes((Vtarget.dof_dset.layout_vec.getSizes(),
                Vdonor.dof_dset.layout_vec.getSizes()))
  mat.setUp()
  mat.setOption(mat.Option.IGNORE_ZERO_ENTRIES, True)
  mat.setFromOptions()

  assert Vdonor.ufl_element() == Vtarget.ufl_element()
  assert type(Vdonor.ufl_element()) == ufl.FiniteElement

  # Interpolate onto a new function in the target space
  X = interpolate(ufl.SpatialCoordinate(Vtarget.mesh()),
                  firedrake.FunctionSpace(Vtarget.mesh(), ufl.VectorElement(Vdonor.ufl_element())))

  # Create the local to global map
  mat.setLGMap(rmap=Vtarget.dof_dset.lgmap,
               cmap=Vdonor.dof_dset.lgmap)

  # Preallocate the matrix
  mat.setPreallocationNNZ(Vdonor.cell_node_map().arity)
  # Set up the local matrix
  local_matrix = numpy.empty(Vtarget.finat_element.space_dimension(), dtype="double")

  # Save the generated c-code as the evaluator to compile the local matrices
  src, evaluator = make_c_evaluate(firedrake.TestFunction(Vdonor))

  # Using the kernel and the C generated code to retrieve the matrix
  cfunction = _CFunction()
  cfunction.n_cols = Vdonor.mesh().num_cells()
  cfunction.n_layers = 1
  cfunction.coords = Vdonor.mesh().coordinates.dat.data.ctypes.data_as(ctypes.POINTER(ctypes.c_double))
  cfunction.coords_map = Vdonor.mesh().coordinates.function_space().cell_node_list.ctypes.data_as(ctypes.POINTER(ctypes.c_int32))
  cfunction.f = None
  cfunction.f_map = None
  cfunction.sidx = Vdonor.mesh().spatial_index.ctypes

  for i, pt in enumerate(X.dat.data_ro):
      cell = Vdonor.mesh().locate_cell(pt)
      cols = Vdonor.cell_node_map().values[cell, ]

      # The array contained in compile_elements is the local matrix
      local_matrix[:] = 0
      evaluator(ctypes.pointer(cfunction), pt.ctypes.data,
                local_matrix.ctypes.data)
      # clamp small values
      local_matrix[numpy.isclose(local_matrix, 0, rtol=1e-12)] = 0

      # Use mat.setValue to insert the local matrix into the global matrix
      mat.setValuesLocal([i], cols, local_matrix, PETSc.InsertMode.INSERT_VALUES)

  # Begin the assembly of the PETSc matrix
  mat.assemblyBegin()

  # End the assembly
  mat.assemblyEnd()

  # Return the Interpolation Matrix
  return mat
