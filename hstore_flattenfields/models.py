#!/usr/bin/env python
# encoding: utf-8

'''
Created on 13/10/2012

@author: iuri
'''
import sys
from django.db import models
from django_orm.postgresql import hstore
from django.utils.datastructures import SortedDict
from django.db.models.fields import FieldDoesNotExist
from django import forms

from datetime import datetime
import json

# import caching.base
import copy

from fields import *
from queryset import *
from utils import *


dfields = None

class DynamicField(models.Model):
    refer = models.CharField(max_length=120, blank=False, db_index=True, verbose_name="Class name")
    name = models.CharField(max_length=120, blank=False, db_index=True, unique=True, verbose_name="Field name")
    verbose_name = models.CharField(max_length=120, blank=False, verbose_name="Verbose name")
    typo = models.CharField(max_length=20, blank=False, db_index=True, verbose_name="Field type",
        choices=single_list_to_tuple(FIELD_TYPES))
    max_length = models.IntegerField(null=True, blank=True, verbose_name="Length")
    order = models.IntegerField(null=True, blank=True, default=None, verbose_name="Order")
    blank = models.BooleanField(default=True, verbose_name="Blank")
    choices = models.TextField(null=True, blank=True, verbose_name="Choices")
    default_value = models.CharField(max_length=80, null=True, blank=True, verbose_name="Default value")

    class Meta:
        db_table = u'dynamic_field'

    def __unicode__(self):
        return self.verbose_name or self.name

    @property
    def has_blank_option(self):
        return self.blank and \
            self.typo not in FIELD_TYPES_WITHOUT_BLANK_OPTION

    def save(self, *args, **kwargs):
        super(DynamicField, self).save(*args, **kwargs)
        # NOTE: force update global dfields fieldset
        global dfields
        dfields =  DynamicField.objects.all()


# XXX: Charge memory with all dfields for prevent flood on db.
dfields =  DynamicField.objects.all()

def find_dfields(refer=None, name=None):

    if name and refer:
        return [dfield for dfield in dfields \
            if dfield.refer == refer and dfield.name == name]
    if name and not refer:
        [dfield for dfield in dfields if dfield.name == name]
    return [dfield for dfield in dfields if dfield.refer == refer]



class HStoreModelMeta(models.Model.__metaclass__):
    def __new__(cls, name, bases, attrs):
        super_new = super(HStoreModelMeta, cls).__new__

        # create it
        new_class = super_new(cls, name, bases, attrs)

        # pos create

        # override getattr/setattr/delattr
        old_getattribute = new_class.__getattribute__
        def __getattribute__(self, key):
            field = None

            try:
                return old_getattribute(self, key)
            except AttributeError:
                field = find_dfields(refer=self.__class__.__name__, name=key)

                if hasattr(self, '_dfields') and key in self._dfields:
                    return self._dfields[key]
                elif field:
                    # FIXME: This Dynamic field really exists
                    return field[0].default_value
                else:
                    raise
            except ValueError:
                if isinstance(field, list) and field:
                    return field[0].default_value
            except TypeError:
                field = find_dfields(refer=self.__class__.__name__, name=key)

                if field and field.__class__.__name__ == 'ManyRelatedManager':
                    return field.all()

                return field

        new_class.__getattribute__ = __getattribute__

        old_setattr = new_class.__setattr__
        def __setattr__(self, key, value):
            #print "called __setattr__(%r, %r)" % (key, value)

            if hasattr(self, '_dfields') and not key in dir(new_class):
                # XXX: search for key on table, django will call this method many times on __init__
                new_class_refer = new_class.__name__
                # if DynamicField.objects.filter(refer=new_class_refer, name=key).exists():
                if find_dfields(refer=new_class_refer, name=key):
                    if isinstance(value, (list, tuple)):
                        value = [unicode(v) for v in value]
                    elif value is not None:
                        value = unicode(value)
                    self._dfields[key] = unicode(value)
                    return

            old_setattr(self, key, value)

        new_class.__setattr__ = __setattr__

        old_delattr = new_class.__delattr__
        def __delattr__(self, key):
            if hasattr(self, '_dfields') and not key in dir(new_class):
                if key in self._dfields:
                    del self._dfields[key]
                    return

            return old_delattr(self, key)
        new_class.__delattr__ = __delattr__

        # override _meta.fields (property)
        _old_meta = new_class._meta
        class _meta(object):
            def __eq__(self, other):
                return _old_meta == other

            def __getattr__(self, key):
                return getattr(_old_meta, key)
            def __setattr__(self, key, value):
                return setattr(_old_meta, key, value)

            def init_name_map(self):
                _cache = _old_meta.init_name_map()

                for dfield in self.dynamic_fields:
                    _cache.update(**{
                        dfield.name: (dfield, _old_meta.concrete_model, True, False)
                    })

                return _cache

            def get_field_by_name(self, name):
                if name is 'pk': name = 'id'

                try:
                    if hasattr(self, '_name_map') and name in self._name_map:
                        return self._name_map[name]
                    else:
                        cache = self.init_name_map()
                        return cache[name]
                except KeyError:
                    raise FieldDoesNotExist('%s has no field named %r'
                            % (self.object_name, name))

            def get_field(self, name, many_to_many=True):
                """
                Returns the requested field by name. Raises FieldDoesNotExist on error.
                """
                to_search = many_to_many and (self.fields + self.many_to_many) or self.fields
                for f in to_search:
                    if f.name == name:
                        return f

                raise FieldDoesNotExist('%s has no field named %r' % (self.object_name, name))

            def get_all_field_names(self):
                return [f.name for f in self.fields if not f.name == '_dfields']

            def get_all_dynamic_field_names(self):
                return [f.name for f in self.dynamic_fields if not f.name == '_dfields']

            @property
            def dynamic_fields(self):
                fields = []

                if not dynamic_field_table_exists():
                    return fields

                # metafields = DynamicField.objects.filter(refer=new_class.__name__)
                metafields = find_dfields(refer=new_class.__name__)
                for metafield in metafields:
                    try:
                        fields.append(
                            crate_field_from_instance(metafield)
                        )
                    except SyntaxError:
                        raise \
                            TypeError(('Cannot create field for %r, maybe type %r ' + \
                                       'is not a django type') % (metafield, metafield.typo))
                return fields

            @property
            def fields(self):
                #add dynamic_fields from table
                return _old_meta.fields + self.dynamic_fields

            def get_base_chain(self, model):
                """
                Returns a list of parent classes leading to 'model' (order from closet
                to most distant ancestor). This has to handle the case were 'model' is
                a granparent or even more distant relation.
                """

                if model in self.parents or not self.parents:
                    # FIXME: In cases of the actual Model doesn`t have
                    # Any parent, so return him
                    return [model]

                parent = None
                for parent in self.parents:
                    res = parent._meta.get_base_chain(model)
                    if res:
                        res.insert(0, parent)
                        return res

                if model.__base__ == parent:
                    return [parent]

                raise TypeError('%r is not an ancestor of this model'
                        % model._meta.module_name)

        new_class._meta = _meta()
        return new_class


class HStoreModel(models.Model):
    __metaclass__ = HStoreModelMeta
    _dfields = hstore.DictionaryField(db_index=True, null=True, blank=True)

    objects = FlattenFieldsFilterManager()

    class Meta:
        abstract = True

    def __init__(self, *args, **kwargs):
        _dfields = None
        if args:
            # XXX: hack in order to save _dfields without alter django
            # save _dfields in args and restore

            # what the index of _dfields?
            i = 0
            index = None
            for f in self._meta.local_fields:
                if f.name == "_dfields":
                    index = i
                    break
                i = i + 1
            if index is not None and index < len(args):
                _dfields = args[index]

        super(HStoreModel, self).__init__(*args, **kwargs)
        if _dfields:
            self._dfields = _dfields

    def __getattr__(self, attr_name):
        """
        This method was override because we have to manipulate the format
        of the data should be shown in the templates. By adding a property
        called 'pretty_FIELDNAME'.
        """
        field_name = attr_name.replace('pretty_', '')
        field = find_dfields(refer=self.__class__.__name__, name=field_name)

        if field:
            field = field[0]
            value = getattr(self, field.name)

            if field.typo in PRETTY_FIELDS:
                if field.typo == "Monetary":
                    new_value = 'R$ %s' % dec2real(value)

                elif field.typo in ["CheckBox", "MultSelect"]:
                    new_value = ", ".join(str2literal(value)) + "."

                elif field.typo == "Date":
                    y, m, d = tuple([int(x) for x in value.split('/')][::-1])
                    new_value = datetime(y, m, d)

                setattr(self, attr_name, new_value)

        return super(HStoreModel, self).__getattribute__(attr_name)
