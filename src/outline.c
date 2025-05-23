/*
 * THIS IS WORK IN PROGRESS.
 *
 * The Python Imaging Library.
 *
 * "arrow" outline stuff.  the contents of this module
 * will be merged with the path module and the rest of
 * the arrow graphics  package, but not before PIL 1.1.
 * use at your own risk.
 *
 * history:
 * 99-01-10 fl  Added to PIL (experimental)
 *
 * Copyright (c) Secret Labs AB 1999.
 * Copyright (c) Fredrik Lundh 1999.
 *
 * See the README file for information on usage and redistribution.
 */

#include "Python.h"

#include "libImaging/Imaging.h"

/* -------------------------------------------------------------------- */
/* Class                                                                */

typedef struct {
    PyObject_HEAD ImagingOutline outline;
} OutlineObject;

static PyTypeObject OutlineType;

#define PyOutline_Check(op) (Py_TYPE(op) == &OutlineType)

static OutlineObject *
_outline_new(void) {
    OutlineObject *self;

    if (PyType_Ready(&OutlineType) < 0) {
        return NULL;
    }

    self = PyObject_New(OutlineObject, &OutlineType);
    if (self == NULL) {
        return NULL;
    }

    self->outline = ImagingOutlineNew();

    return self;
}

static void
_outline_dealloc(OutlineObject *self) {
    ImagingOutlineDelete(self->outline);
    PyObject_Del(self);
}

ImagingOutline
PyOutline_AsOutline(PyObject *outline) {
    if (PyOutline_Check(outline)) {
        return ((OutlineObject *)outline)->outline;
    }

    return NULL;
}

/* -------------------------------------------------------------------- */
/* Factories                                                            */

PyObject *
PyOutline_Create(PyObject *self, PyObject *args) {
    if (!PyArg_ParseTuple(args, ":outline")) {
        return NULL;
    }

    return (PyObject *)_outline_new();
}

/* -------------------------------------------------------------------- */
/* Methods                                                              */

static PyObject *
_outline_move(OutlineObject *self, PyObject *args) {
    float x0, y0;
    if (!PyArg_ParseTuple(args, "ff", &x0, &y0)) {
        return NULL;
    }

    ImagingOutlineMove(self->outline, x0, y0);

    Py_RETURN_NONE;
}

static PyObject *
_outline_line(OutlineObject *self, PyObject *args) {
    float x1, y1;
    if (!PyArg_ParseTuple(args, "ff", &x1, &y1)) {
        return NULL;
    }

    ImagingOutlineLine(self->outline, x1, y1);

    Py_RETURN_NONE;
}

static PyObject *
_outline_curve(OutlineObject *self, PyObject *args) {
    float x1, y1, x2, y2, x3, y3;
    if (!PyArg_ParseTuple(args, "ffffff", &x1, &y1, &x2, &y2, &x3, &y3)) {
        return NULL;
    }

    ImagingOutlineCurve(self->outline, x1, y1, x2, y2, x3, y3);

    Py_RETURN_NONE;
}

static PyObject *
_outline_close(OutlineObject *self, PyObject *args) {
    if (!PyArg_ParseTuple(args, ":close")) {
        return NULL;
    }

    ImagingOutlineClose(self->outline);

    Py_RETURN_NONE;
}

static PyObject *
_outline_transform(OutlineObject *self, PyObject *args) {
    double a[6];
    if (!PyArg_ParseTuple(args, "(dddddd)", a + 0, a + 1, a + 2, a + 3, a + 4, a + 5)) {
        return NULL;
    }

    ImagingOutlineTransform(self->outline, a);

    Py_RETURN_NONE;
}

static struct PyMethodDef _outline_methods[] = {
    {"line", (PyCFunction)_outline_line, METH_VARARGS},
    {"curve", (PyCFunction)_outline_curve, METH_VARARGS},
    {"move", (PyCFunction)_outline_move, METH_VARARGS},
    {"close", (PyCFunction)_outline_close, METH_VARARGS},
    {"transform", (PyCFunction)_outline_transform, METH_VARARGS},
    {NULL, NULL} /* sentinel */
};

static PyTypeObject OutlineType = {
    PyVarObject_HEAD_INIT(NULL, 0).tp_name = "Outline",
    .tp_basicsize = sizeof(OutlineObject),
    .tp_dealloc = (destructor)_outline_dealloc,
    .tp_methods = _outline_methods,
};
