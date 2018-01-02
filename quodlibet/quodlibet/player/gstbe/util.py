# -*- coding: utf-8 -*-
# Copyright 2009-2011 Steven Robertson, Christoph Reiter
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.

import collections
import subprocess

from gi.repository import GLib, Gst

from quodlibet import _
from quodlibet.util.string import decode
from quodlibet.util import is_linux, is_windows
from quodlibet.player import PlayerError
from quodlibet.compat import text_type, number_types, xrange


def pulse_is_running():
    """Returns whether pulseaudio is running"""

    # If we have a pulsesink we can get the server presence through
    # setting the ready state
    element = Gst.ElementFactory.make("pulsesink", None)
    if element is not None:
        element.set_state(Gst.State.READY)
        res = element.get_state(0)[0]
        element.set_state(Gst.State.NULL)
        return res != Gst.StateChangeReturn.FAILURE

    # In case we don't have it call the pulseaudio binary
    try:
        subprocess.check_call(["pulseaudio", "--check"])
    except subprocess.CalledProcessError:
        return False
    except OSError:
        return False
    return True


def link_many(elements):
    last = None
    for element in elements:
        if last:
            if not Gst.Element.link(last, element):
                return False
        last = element
    return True


def unlink_many(elements):
    last = None
    for element in elements:
        if last:
            if not Gst.Element.unlink(last, element):
                return False
        last = element
    return True


def iter_to_list(func):
    objects = []

    iter_ = func()
    while 1:
        status, value = iter_.next()
        if status == Gst.IteratorResult.OK:
            objects.append(value)
        else:
            break
    return objects


def find_audio_sink():
    """Get the best audio sink available.

    Returns (element, description) or raises PlayerError.
    """

    if is_windows():
        sinks = [
            "directsoundsink",
            "autoaudiosink",
        ]
    elif is_linux() and pulse_is_running():
        sinks = [
            "pulsesink",
        ]
    else:
        sinks = [
            "autoaudiosink",  # plugins-good
            "pulsesink",  # plugins-good
            "alsasink",  # plugins-base
        ]

    for name in sinks:
        element = Gst.ElementFactory.make(name, None)
        if element is not None:
            return (element, name)
    else:
        details = " (%s)" % ", ".join(sinks) if sinks else ""
        raise PlayerError(_("No GStreamer audio sink found") + details)


def GStreamerSink(pipeline_desc):
    """Returns a list of unlinked gstreamer elements ending with an audio sink
    and a textual description of the pipeline.

    `pipeline_desc` can be gst-launch syntax for multiple elements
    with or without an audiosink.

    In case of an error, raises PlayerError
    """

    pipe = None
    if pipeline_desc:
        try:
            pipe = [Gst.parse_launch(e) for e in pipeline_desc.split('!')]
        except GLib.GError as e:
            message = e.message
            raise PlayerError(_("Invalid GStreamer output pipeline"), message)

    if pipe:
        # In case the last element is linkable with a fakesink
        # it is not an audiosink, so we append the default one
        fake = Gst.ElementFactory.make('fakesink', None)
        if link_many([pipe[-1], fake]):
            unlink_many([pipe[-1], fake])
            default_elm, default_desc = find_audio_sink()
            pipe += [default_elm]
            pipeline_desc += " ! " + default_desc
    else:
        elm, pipeline_desc = find_audio_sink()
        pipe = [elm]

    return pipe, pipeline_desc


class TagListWrapper(collections.Mapping):
    def __init__(self, taglist, merge=False):
        self._list = taglist
        self._merge = merge

    def __len__(self):
        return self._list.n_tags()

    def __iter__(self):
        for i in xrange(len(self)):
            yield self._list.nth_tag_name(i)

    def __getitem__(self, key):
        if not Gst.tag_exists(key):
            raise KeyError

        values = []
        index = 0
        while 1:
            value = self._list.get_value_index(key, index)
            if value is None:
                break
            values.append(value)
            index += 1

        if not values:
            raise KeyError

        if self._merge:
            try:
                return " - ".join(values)
            except TypeError:
                return values[0]

        return values


def parse_gstreamer_taglist(tags):
    """Takes a GStreamer taglist and returns a dict containing only
    numeric and unicode values and str keys."""

    merged = {}
    for key in tags.keys():
        value = tags[key]
        # extended-comment sometimes containes a single vorbiscomment or
        # a list of them ["key=value", "key=value"]
        if key == "extended-comment":
            if not isinstance(value, list):
                value = [value]
            for val in value:
                if not isinstance(val, text_type):
                    continue
                split = val.split("=", 1)
                sub_key = split[0]
                val = split[-1]
                if sub_key in merged:
                    sub_val = merged[sub_key]
                    if not isinstance(sub_val, text_type):
                        continue
                    if val not in sub_val.split("\n"):
                        merged[sub_key] += "\n" + val
                else:
                    merged[sub_key] = val
        elif isinstance(value, Gst.DateTime):
            value = value.to_iso8601_string()
            merged[key] = value
        else:
            if isinstance(value, number_types):
                merged[key] = value
                continue

            if isinstance(value, bytes):
                value = decode(value)

            if not isinstance(value, text_type):
                value = text_type(value)

            if key in merged:
                merged[key] += "\n" + value
            else:
                merged[key] = value

    return merged


def bin_debug(elements, depth=0, lines=None):
    """Takes a list of gst.Element that are part of a prerolled pipeline, and
    recursively gets the children and all caps between the elements.

    Returns a list of text lines suitable for printing.
    """

    from quodlibet.util.dprint import Colorise

    if lines is None:
        lines = []
    else:
        lines.append(" " * (depth - 1) + "\\")

    for i, elm in enumerate(elements):
        for pad in iter_to_list(elm.iterate_sink_pads):
            caps = pad.get_current_caps()
            if caps:
                lines.append("%s| %s" % (" " * depth, caps.to_string()))
        name = elm.get_name()
        cls = Colorise.blue(type(elm).__name__.split(".", 1)[-1])
        lines.append("%s|-%s (%s)" % (" " * depth, cls, name))

        if isinstance(elm, Gst.Bin):
            children = reversed(iter_to_list(elm.iterate_sorted))
            bin_debug(children, depth + 1, lines)

    return lines
