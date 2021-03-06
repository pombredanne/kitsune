from collections import namedtuple
from datetime import datetime
from itertools import chain
from urlparse import urlparse

from django.conf import settings
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError, ObjectDoesNotExist
from django.core.urlresolvers import resolve
from django.db import models
from django.db.models import Q
from django.http import Http404

from pyquery import PyQuery
from tidings.models import NotificationsMixin
from tower import ugettext_lazy as _lazy, ugettext as _

from search import S
from search.utils import crc32
from sumo import ProgrammingError
from sumo_locales import LOCALES
from sumo.models import ModelBase, LocaleField
from sumo.urlresolvers import reverse, split_path
from tags.models import BigVocabTaggableMixin
from wiki import TEMPLATE_TITLE_PREFIX


# Disruptiveness of edits to translated versions. Numerical magnitude indicate
# the relative severity.
TYPO_SIGNIFICANCE = 10
MEDIUM_SIGNIFICANCE = 20
MAJOR_SIGNIFICANCE = 30

SIGNIFICANCES = [
    (TYPO_SIGNIFICANCE,
     _lazy(u'Minor details like punctuation and spelling errors')),
    (MEDIUM_SIGNIFICANCE,
     _lazy(u"Content changes that don't require immediate translation")),
    (MAJOR_SIGNIFICANCE,
     _lazy(u'Major content changes that will make older translations '
           'inaccurate')),
]

SIGNIFICANCES_HELP = {
    TYPO_SIGNIFICANCE:
        _lazy(u'These changes are inconsequential to localizers: they will '
              'not be notified of the change and it will not affect '
              'translations of the article.'),
    MEDIUM_SIGNIFICANCE:
        _lazy(u'This will notify localizers and translations will be marked '
              'as "needing updates" on dashboards. Most '
              'changes&mdash;updating an image, fixing {for} markup, adding '
              'or removing non-critical sections&mdash;should use this.'),
    MAJOR_SIGNIFICANCE:
        _lazy(u'This will notify localizers and translations will be marked '
              '"out of date" on dashboards. Translations will show a warning '
              'to users that they are out of date and that the English '
              'version is the most accurate. Use this when the old '
              'instructions are completely unusable.'),
}

CATEGORIES = (
    (10, _lazy(u'Troubleshooting')),
    (20, _lazy(u'How to')),
    (30, _lazy(u'How to contribute')),
    (40, _lazy(u'Administration')),
    (50, _lazy(u'Navigation')),
    (60, _lazy(u'Templates')),
)

# FF versions used to filter article searches, power {for} tags, etc.:
#
# Iterables of (ID, name, abbreviation for {for} tags, max version this version
# group encompasses, whether-this-version-should-show-in-the-menu, and whether-
# this-version-is-the-default-on-this-platform), grouped into optgroups by
# platform. To add the ability to sniff a new version of an existing browser
# (assuming it doesn't change the user agent string too radically), you should
# need only to add a line here; no JS required. Just be wary of inexact
# floating point comparisons when setting max_version, which should be read as
# "From the next smaller max_version up to but not including version x.y".
#
# When a wiki page is being viewed in a desktop browser, the {for} sections for
# the default mobile browser still show. The reverse is true when a page is
# being viewed in a mobile browser.
VersionMetadata = namedtuple('VersionMetadata',
                             'id, name, long, slug, max_version, show_in_ui, '
                             'is_default')
GROUPED_FIREFOX_VERSIONS = (
    ((_lazy(u'Desktop:'), 'desktop'), (
        VersionMetadata(13, _lazy(u'Firefox 9'),
                        _lazy(u'Firefox 9'), 'fx9', 9.9999, True, False),
        VersionMetadata(11, _lazy(u'Firefox 8'),
                        _lazy(u'Firefox 8'), 'fx8', 8.9999, True, True),
        VersionMetadata(9, _lazy(u'Firefox 7'),
                        _lazy(u'Firefox 7'), 'fx7', 7.9999, True, False),
        VersionMetadata(6, _lazy(u'Firefox 6'),
                        _lazy(u'Firefox 6'), 'fx6', 6.9999, False, False),
        VersionMetadata(5, _lazy(u'Firefox 5'),
                        _lazy(u'Firefox 5'), 'fx5', 5.9999, False, False),
        VersionMetadata(1, _lazy(u'Firefox 4'),
                        _lazy(u'Firefox 4'), 'fx4', 4.9999, False, False),
        VersionMetadata(2, _lazy(u'Firefox 3.5-3.6'),
                        _lazy(u'Firefox 3.5-3.6'), 'fx35', 3.9999, True,
                        False),
        VersionMetadata(3, _lazy(u'Firefox 3.0'),
                        _lazy(u'Firefox 3.0'), 'fx3', 3.4999, False, False))),
    ((_lazy(u'Mobile:'), 'mobile'), (
        VersionMetadata(14, _lazy(u'Firefox 9'),
                        _lazy(u'Firefox 9 for Mobile'), 'm9', 9.9999, True,
                        False),
        VersionMetadata(12, _lazy(u'Firefox 8'),
                        _lazy(u'Firefox 8 for Mobile'), 'm8', 8.9999, True,
                        True),
        VersionMetadata(10, _lazy(u'Firefox 7'),
                        _lazy(u'Firefox 7 for Mobile'), 'm7', 7.9999, True,
                        False),
        VersionMetadata(8, _lazy(u'Firefox 6'),
                        _lazy(u'Firefox 6 for Mobile'), 'm6', 6.9999, False,
                        False),
        VersionMetadata(7, _lazy(u'Firefox 5'),
                        _lazy(u'Firefox 5 for Mobile'), 'm5', 5.9999, False,
                        False),
        VersionMetadata(4, _lazy(u'Firefox 4'),
                        _lazy(u'Firefox 4 for Mobile'), 'm4', 4.9999, False,
                        False),)))

# Flattened:
# TODO: Perhaps use optgroups everywhere instead.
FIREFOX_VERSIONS = tuple(chain(*[options for label, options in
                                 GROUPED_FIREFOX_VERSIONS]))

# OSes used to filter articles and declare {for} sections:
OsMetaData = namedtuple('OsMetaData', 'id, name, slug, is_default')
GROUPED_OPERATING_SYSTEMS = (
    ((_lazy(u'Desktop OS:'), 'desktop'), (
        OsMetaData(1, _lazy(u'Windows'), 'win', True),
        OsMetaData(2, _lazy(u'Mac OS X'), 'mac', False),
        OsMetaData(3, _lazy(u'Linux'), 'linux', False))),
    ((_lazy(u'Mobile OS:'), 'mobile'), (
        OsMetaData(5, _lazy(u'Android'), 'android', True),
        OsMetaData(4, _lazy(u'Maemo'), 'maemo', False))))

# Flattened
OPERATING_SYSTEMS = tuple(chain(*[options for label, options in
                                  GROUPED_OPERATING_SYSTEMS]))

# Products supported
Product = namedtuple('Product', 'slug, name')  # slug is used for tag/topic
PRODUCTS = (
    Product('desktop', _lazy(u'Desktop')),
    Product('mobile', _lazy(u'Mobile')),
    Product('sync', _lazy(u'Sync')),
    Product('FxHome', _lazy(u'Home')))
PRODUCT_TAGS = [p.slug for p in PRODUCTS]


REDIRECT_HTML = '<p>REDIRECT <a '  # how a redirect looks as rendered HTML
REDIRECT_CONTENT = 'REDIRECT [[%s]]'
REDIRECT_TITLE = _lazy(u'%(old)s Redirect %(number)i')
REDIRECT_SLUG = _lazy(u'%(old)s-redirect-%(number)i')


class TitleCollision(Exception):
    """An attempt to create two pages of the same title in one locale"""


class SlugCollision(Exception):
    """An attempt to create two pages of the same slug in one locale"""


class _NotDocumentView(Exception):
    """A URL not pointing to the document view was passed to from_url()."""


class Document(NotificationsMixin, ModelBase, BigVocabTaggableMixin):
    """A localized knowledgebase document, not revision-specific."""
    title = models.CharField(max_length=255, db_index=True)
    slug = models.CharField(max_length=255, db_index=True)

    # Is this document a template or not?
    is_template = models.BooleanField(default=False, editable=False,
                                      db_index=True)
    # Is this document localizable or not?
    is_localizable = models.BooleanField(default=True, db_index=True)

    # TODO: validate (against settings.SUMO_LANGUAGES?)
    locale = LocaleField(default=settings.WIKI_DEFAULT_LANGUAGE, db_index=True)

    # Latest approved revision. L10n dashboard depends on this being so (rather
    # than being able to set it to earlier approved revisions). (Remove "+" to
    # enable reverse link.)
    current_revision = models.ForeignKey('Revision', null=True,
                                         related_name='current_for+')

    # Latest revision which both is_approved and is_ready_for_localization,
    # This may remain non-NULL even if is_localizable is changed to false.
    latest_localizable_revision = models.ForeignKey(
        'Revision', null=True, related_name='localizable_for+')

    # The Document I was translated from. NULL iff this doc is in the default
    # locale or it is nonlocalizable. TODO: validate against
    # settings.WIKI_DEFAULT_LANGUAGE.
    parent = models.ForeignKey('self', related_name='translations',
                               null=True, blank=True)

    # Related documents, based on tags in common.
    # The RelatedDocument table is populated by
    # wiki.cron.calculate_related_documents.
    related_documents = models.ManyToManyField('self',
                                               through='RelatedDocument',
                                               symmetrical=False)

    # Cached HTML rendering of approved revision's wiki markup:
    html = models.TextField(editable=False)

    # A document's category must always be that of its parent. If it has no
    # parent, it can do what it wants. This invariant is enforced in save().
    category = models.IntegerField(choices=CATEGORIES, db_index=True)

    # A document's is_archived flag must match that of its parent. If it has no
    # parent, it can do what it wants. This invariant is enforced in save().
    is_archived = models.BooleanField(
        default=False, db_index=True, verbose_name='is obsolete',
        help_text=_lazy(
            u'If checked, this wiki page will be hidden from basic searches '
             'and dashboards. When viewed, the page will warn that it is no '
             'longer maintained.'))

    # Enable discussion (kbforum) on this document.
    allow_discussion = models.BooleanField(
        default=True, help_text=_lazy(
            u'If checked, this document allows discussion in an associated '
             'forum. Uncheck to hide/disable the forum.'))

    # List of users that have contributed to this document.
    contributors = models.ManyToManyField(User)

    # firefox_versions,
    # operating_systems:
    #    defined in the respective classes below. Use them as in
    #    test_firefox_versions.

    # TODO: Rethink indexes once controller code is near complete. Depending on
    # how MySQL uses indexes, we probably don't need individual indexes on
    # title and locale as well as a combined (title, locale) one.
    class Meta(object):
        unique_together = (('parent', 'locale'), ('title', 'locale'),
                           ('slug', 'locale'))
        permissions = [('archive_document', 'Can archive document')]

    class SphinxMeta(object):
        index = 'wiki_pages'
        filter_mapping = {'locale': crc32, 'tag': crc32}

    def _collides(self, attr, value):
        """Return whether there exists a doc in this locale whose `attr` attr
        is equal to mine."""
        return Document.uncached.filter(locale=self.locale,
                                        **{attr: value}).exists()

    def _raise_if_collides(self, attr, exception):
        """Raise an exception if a page of this title/slug already exists."""
        if self.id is None or hasattr(self, 'old_' + attr):
            # If I am new or my title/slug changed...
            if self._collides(attr, getattr(self, attr)):
                raise exception

    def clean(self):
        """Translations can't be localizable."""
        self._clean_is_localizable()
        self._clean_category()
        self._ensure_inherited_attr('is_archived')

    def _clean_is_localizable(self):
        """is_localizable == allowed to have translations. Make sure that isn't
        violated.

        For default language (en-US), is_localizable means it can have
        translations. Enforce:
            * is_localizable=True if it has translations
            * if has translations, unable to make is_localizable=False

        For non-default langauges, is_localizable must be False.

        """
        if self.locale != settings.WIKI_DEFAULT_LANGUAGE:
            self.is_localizable = False

        # Can't save this translation if parent not localizable
        if self.parent and not self.parent.is_localizable:
            raise ValidationError('"%s": parent "%s" is not localizable.' % (
                                  unicode(self), unicode(self.parent)))

        # Can't make not localizable if it has translations
        # This only applies to documents that already exist, hence self.pk
        # TODO: Use uncached manager here, if we notice problems
        if self.pk and not self.is_localizable and self.translations.exists():
            raise ValidationError('"%s": document has %s translations but is '
                                  'not localizable.' % (
                                  unicode(self), self.translations.count()))

    def _ensure_inherited_attr(self, attr):
        """Make sure my `attr` attr is the same as my parent's if I have one.

        Otherwise, if I have children, make sure their `attr` attr is the same
        as mine.

        """
        if self.parent:
            # We always set the child according to the parent rather than vice
            # versa, because we do not expose an Archived checkbox in the
            # translation UI.
            setattr(self, attr, getattr(self.parent, attr))
        else:  # An article cannot have both a parent and children.
            # Make my children the same as me:
            if self.id:
                self.translations.all().update(**{attr: getattr(self, attr)})

    def _clean_category(self):
        """Make sure a doc's category is the same as its parent's."""
        if (not self.parent and
            self.category not in (id for id, name in CATEGORIES)):
            # All we really need to do here is make sure category != '' (which
            # is what it is when it's missing from the DocumentForm). The extra
            # validation is just a nicety.
            raise ValidationError(_('Please choose a category.'))
        self._ensure_inherited_attr('category')

    def _attr_for_redirect(self, attr, template):
        """Return the slug or title for a new redirect.

        `template` is a Python string template with "old" and "number" tokens
        used to create the variant.

        """
        def unique_attr():
            """Return a variant of getattr(self, attr) such that there is no
            Document of my locale with string attribute `attr` equal to it.

            Never returns the original attr value.

            """
            # "My God, it's full of race conditions!"
            i = 1
            while True:
                new_value = template % dict(old=getattr(self, attr), number=i)
                if not self._collides(attr, new_value):
                    return new_value
                i += 1

        old_attr = 'old_' + attr
        if hasattr(self, old_attr):
            # My slug (or title) is changing; we can reuse it for the redirect.
            return getattr(self, old_attr)
        else:
            # Come up with a unique slug (or title):
            return unique_attr()

    def save(self, *args, **kwargs):
        self.is_template = self.title.startswith(TEMPLATE_TITLE_PREFIX)

        self._raise_if_collides('slug', SlugCollision)
        self._raise_if_collides('title', TitleCollision)

        # These are too important to leave to a (possibly omitted) is_valid
        # call:
        self._clean_is_localizable()
        self._ensure_inherited_attr('is_archived')
        # Everything is validated before save() is called, so the only thing
        # that could cause save() to exit prematurely would be an exception,
        # which would cause a rollback, which would negate any category changes
        # we make here, so don't worry:
        self._clean_category()

        super(Document, self).save(*args, **kwargs)

        # Make redirects if there's an approved revision and title or slug
        # changed. Allowing redirects for unapproved docs would (1) be of
        # limited use and (2) require making Revision.creator nullable.
        slug_changed = hasattr(self, 'old_slug')
        title_changed = hasattr(self, 'old_title')
        if self.current_revision and (slug_changed or title_changed):
            doc = Document.objects.create(locale=self.locale,
                                          title=self._attr_for_redirect(
                                              'title', REDIRECT_TITLE),
                                          slug=self._attr_for_redirect(
                                              'slug', REDIRECT_SLUG),
                                          category=self.category,
                                          is_localizable=False)
            Revision.objects.create(document=doc,
                                    content=REDIRECT_CONTENT % self.title,
                                    is_approved=True,
                                    reviewer=self.current_revision.creator,
                                    creator=self.current_revision.creator)

            if slug_changed:
                del self.old_slug
            if title_changed:
                del self.old_title

    def __setattr__(self, name, value):
        """Trap setting slug and title, recording initial value."""
        # Public API: delete the old_title or old_slug attrs after changing
        # title or slug (respectively) to suppress redirect generation.
        if getattr(self, 'id', None):
            # I have been saved and so am worthy of a redirect.
            if name in ('slug', 'title') and hasattr(self, name):
                old_name = 'old_' + name
                if not hasattr(self, old_name):
                    # Case insensitive comparison:
                    if getattr(self, name).lower() != value.lower():
                        # Save original value:
                        setattr(self, old_name, getattr(self, name))
                elif value == getattr(self, old_name):
                    # They changed the attr back to its original value.
                    delattr(self, old_name)
        super(Document, self).__setattr__(name, value)

    @property
    def content_parsed(self):
        if not self.current_revision:
            return ''
        return self.current_revision.content_parsed

    @property
    def language(self):
        return settings.LANGUAGES[self.locale.lower()]

    def get_absolute_url(self):
        return reverse('wiki.document', locale=self.locale, args=[self.slug])

    @staticmethod
    def from_url(url, required_locale=None, id_only=False, check_host=True):
        """Return the approved Document the URL represents, None if there isn't
        one.

        Return None if the URL is a 404, the URL doesn't point to the right
        view, or the indicated document doesn't exist.

        To limit the universe of discourse to a certain locale, pass in a
        `required_locale`. To fetch only the ID of the returned Document, set
        `id_only` to True.

        If the URL has a host component, we assume it does not point to this
        host and thus does not point to a Document, because that would be a
        needlessly verbose way to specify an internal link. However, if you
        pass host_safe=True, we assume the URL's host is the one serving
        Documents, which comes in handy for analytics whose metrics return
        host-having URLs.

        """
        try:
            components = _doc_components_from_url(
                url, required_locale=required_locale, check_host=check_host)
        except _NotDocumentView:
            return None
        if not components:
            return None
        locale, path, slug = components

        # Map locale-slug pair to Document ID:
        doc_query = Document.objects.exclude(current_revision__isnull=True)
        if id_only:
            doc_query = doc_query.only('id')
        try:
            return doc_query.get(locale=locale, slug=slug)
        except Document.DoesNotExist:
            return None

    def redirect_url(self):
        """If I am a redirect, return the URL to which I redirect.

        Otherwise, return None.

        """
        # If a document starts with REDIRECT_HTML and contains any <a> tags
        # with hrefs, return the href of the first one. This trick saves us
        # from having to parse the HTML every time.
        if self.html.startswith(REDIRECT_HTML):
            anchors = PyQuery(self.html)('a[href]')
            if anchors:
                return anchors[0].get('href')

    def redirect_document(self):
        """If I am a redirect to a Document, return that Document.

        Otherwise, return None.

        """
        url = self.redirect_url()
        if url:
            return self.from_url(url)

    def __unicode__(self):
        return '[%s] %s' % (self.locale, self.title)

    def allows_revision_by(self, user):
        """Return whether `user` is allowed to create new revisions of me.

        The motivation behind this method is that templates and other types of
        docs may have different permissions.

        """
        # TODO: Add tests for templateness or whatever is required.
        # Leaving this method signature untouched for now in case we do need
        # to use it in the future. ~james
        return True

    def allows_editing_by(self, user):
        """Return whether `user` is allowed to edit document-level metadata.

        If the Document doesn't have a current_revision (nothing approved) then
        all the Document fields are still editable. Once there is an approved
        Revision, the Document fields can only be edited by privileged users.

        """
        return (not self.current_revision or
                user.has_perm('wiki.change_document'))

    def allows_deleting_by(self, user):
        """Return whether `user` is allowed to delete this document."""
        return (user.has_perm('wiki.delete_document') or
                user.has_perm('wiki.delete_document_{locale}'.format(
                              locale=self.locale)))

    def allows_vote(self, request):
        """Return whether `user` can vote on this document."""
        return (not self.is_archived and self.current_revision and
                not self.current_revision.has_voted(request))

    def translated_to(self, locale):
        """Return the translation of me to the given locale.

        If there is no such Document, return None.

        """
        if self.locale != settings.WIKI_DEFAULT_LANGUAGE:
            raise NotImplementedError('translated_to() is implemented only on'
                                      'Documents in the default language so'
                                      'far.')
        try:
            return Document.objects.get(locale=locale, parent=self)
        except Document.DoesNotExist:
            return None

    @property
    def original(self):
        """Return the document I was translated from or, if none, myself."""
        return self.parent or self

    def localizable_or_latest_revision(self, include_rejected=False):
        """Return latest ready-to-localize revision if there is one,
        else the latest approved revision if there is one,
        else the latest unrejected (unreviewed) revision if there is one,
        else None.

        include_rejected -- If true, fall back to the latest rejected
            revision if all else fails.

        """
        def latest(queryset):
            """Return the latest item from a queryset (by ID).

            Return None if the queryset is empty.

            """
            try:
                return queryset.order_by('-id')[0:1].get()
            except ObjectDoesNotExist:  # Catching IndexError seems overbroad.
                return None

        rev = self.latest_localizable_revision
        if not rev or not self.is_localizable:
            rejected = Q(is_approved=False, reviewed__isnull=False)

            # Try latest approved revision:
            rev = (latest(self.revisions.filter(is_approved=True)) or
                   # No approved revs. Try unrejected:
                   latest(self.revisions.exclude(rejected)) or
                   # No unrejected revs. Maybe fall back to rejected:
                   (latest(self.revisions) if include_rejected else None))
        return rev

    def is_majorly_outdated(self):
        """Return whether a MAJOR_SIGNIFICANCE-level update has occurred to the
        parent document since this translation had an approved update and such
        revision is ready for l10n.

        If this is not a translation or has never been approved, return False.

        """
        if not (self.parent and self.current_revision):
            return False

        based_on_id = self.current_revision.based_on_id
        more_filters = {'id__gt': based_on_id} if based_on_id else {}
        return self.parent.revisions.filter(
            is_approved=True, is_ready_for_localization=True,
            significance__gte=MAJOR_SIGNIFICANCE, **more_filters).exists()

    def is_watched_by(self, user):
        """Return whether `user` is notified of edits to me."""
        from wiki.events import EditDocumentEvent
        return EditDocumentEvent.is_notifying(user, self)


class Revision(ModelBase):
    """A revision of a localized knowledgebase document"""
    document = models.ForeignKey(Document, related_name='revisions')
    summary = models.TextField()  # wiki markup
    content = models.TextField()  # wiki markup

    # Keywords are used mostly to affect search rankings. Moderators may not
    # have the language expertise to translate keywords, so we put them in the
    # Revision so the translators can handle them:
    keywords = models.CharField(max_length=255, blank=True)

    created = models.DateTimeField(default=datetime.now)
    reviewed = models.DateTimeField(null=True)

    # The significance of the initial revision of a document is NULL.
    significance = models.IntegerField(choices=SIGNIFICANCES, null=True)

    comment = models.CharField(max_length=255)
    reviewer = models.ForeignKey(User, related_name='reviewed_revisions',
                                 null=True)
    creator = models.ForeignKey(User, related_name='created_revisions')
    is_approved = models.BooleanField(default=False, db_index=True)

    # The default locale's rev that was the latest ready-for-l10n one when the
    # Edit button was hit to begin creating this revision. If there was none,
    # this is simply the latest of the default locale's revs as of that time.
    # Used to determine whether localizations are out of date.
    based_on = models.ForeignKey('self', null=True, blank=True)
    # TODO: limit_choices_to={'document__locale':
    # settings.WIKI_DEFAULT_LANGUAGE} is a start but not sufficient.

    # Is both approved and marked as ready for translation (which will result
    # in the translation UI considering it when looking for the latest
    # translatable version). If is_approved=False or this revision belongs to a
    # non-default-language Document, this must be False.
    is_ready_for_localization = models.BooleanField(default=False)

    class Meta(object):
        permissions = [('review_revision', 'Can review a revision'),
                       ('mark_ready_for_l10n',
                        'Can mark revision as ready for localization')]

    def _based_on_is_clean(self):
        """Return a tuple: (the correct value of based_on, whether the old
        value was correct).

        based_on must be a revision of the English version of the document. If
        based_on is not already set when this is called, the return value
        defaults to something reasonable.

        """
        original = self.document.original
        if self.based_on and self.based_on.document != original:
            # based_on is set and points to the wrong doc. The following is
            # then the most likely helpful value:
            return original.localizable_or_latest_revision(), False
        # Even None is permissible, for example in the case of a brand new doc.
        return self.based_on, True

    def clean(self):
        """Ensure based_on is valid & police is_ready/is_approved invariant."""
        # All of the cleaning herein should be unnecessary unless the user
        # messes with hidden form data.
        try:
            self.document and self.document.original
        except Document.DoesNotExist:
            # For clean()ing forms that don't have a document instance behind
            # them yet
            self.based_on = None
        else:
            based_on, is_clean = self._based_on_is_clean()
            if not is_clean:
                old = self.based_on
                self.based_on = based_on  # Be nice and guess a correct value.
                # TODO(erik): This error message ignores non-translations.
                raise ValidationError(_('A revision must be based on a '
                    'revision of the %(locale)s document. Revision ID'
                    ' %(id)s does not fit those criteria.') %
                    dict(locale=LOCALES[settings.WIKI_DEFAULT_LANGUAGE].native,
                         id=old.id))

        if not self.can_be_readied_for_localization():
            self.is_ready_for_localization = False

    def save(self, *args, **kwargs):
        _, is_clean = self._based_on_is_clean()
        if not is_clean:  # No more Mister Nice Guy
            # TODO(erik): This error message ignores non-translations.
            raise ProgrammingError('Revision.based_on must be None or refer '
                                   'to a revision of the default-'
                                   'language document.')

        super(Revision, self).save(*args, **kwargs)

        # When a revision is approved, re-cache the document's html content
        # and update document contributors
        if self.is_approved and (
                not self.document.current_revision or
                self.document.current_revision.id < self.id):
            # Determine if there are new contributors and add them to the list
            contributors = self.document.contributors.all()
            # Exclude all explicitly rejected revisions
            new_revs = self.document.revisions.exclude(
                reviewed__isnull=False, is_approved=False)
            if self.document.current_revision:
                new_revs = new_revs.filter(
                    id__gt=self.document.current_revision.id)
            new_contributors = set([r.creator
                for r in new_revs.select_related('creator')])
            for user in new_contributors:
                if user not in contributors:
                    self.document.contributors.add(user)

            # Update document denormalized fields
            if self.is_ready_for_localization:
                self.document.latest_localizable_revision = self
            self.document.html = self.content_parsed
            self.document.current_revision = self
            self.document.save()
        elif (self.is_ready_for_localization and
              (not self.document.latest_localizable_revision or
               self.id > self.document.latest_localizable_revision.id)):
            # We are marking a newer revision as ready for l10n.
            # Update the denormalized field on the document.
            self.document.latest_localizable_revision = self
            self.document.save()

    def delete(self, *args, **kwargs):
        """Dodge cascading delete of documents and other revisions."""
        def latest_revision(excluded_rev, constraint):
            """Return the largest-ID'd revision meeting the given constraint
            and excluding the given revision, or None if there is none."""
            revs = document.revisions.filter(constraint).exclude(
                pk=excluded_rev.pk).order_by('-id')[:1]
            try:
                # Academic TODO: There's probably a way to keep the QuerySet
                # lazy all the way through the update() call.
                return revs[0]
            except IndexError:
                return None

        Revision.objects.filter(based_on=self).update(based_on=None)
        document = self.document

        # If the current_revision is being deleted, try to update it to the
        # previous approved revision:
        if document.current_revision == self:
            new_current = latest_revision(self, Q(is_approved=True))
            document.update(
                current_revision=new_current,
                html=new_current.content_parsed if new_current else '')

        # Likewise, step the latest_localizable_revision field backward if
        # we're deleting that revision:
        if document.latest_localizable_revision == self:
            document.update(latest_localizable_revision=latest_revision(
                self, Q(is_approved=True, is_ready_for_localization=True)))

        super(Revision, self).delete(*args, **kwargs)

    def has_voted(self, request):
        """Did the user already vote for this revision?"""
        if request.user.is_authenticated():
            qs = HelpfulVote.objects.filter(revision=self,
                                            creator=request.user)
        elif request.anonymous.has_id:
            anon_id = request.anonymous.anonymous_id
            qs = HelpfulVote.objects.filter(revision=self,
                                            anonymous_id=anon_id)
        else:
            return False

        return qs.exists()

    def __unicode__(self):
        return u'[%s] %s #%s: %s' % (self.document.locale,
                                      self.document.title,
                                      self.id, self.content[:50])

    @property
    def content_parsed(self):
        from wiki.parser import wiki_to_html
        return wiki_to_html(self.content, locale=self.document.locale,
                            doc_id=self.document.id)

    def can_be_readied_for_localization(self):
        """Return whether this revision has the prerequisites necessary for the
        user to mark it as ready for localization."""
        # If not is_approved, can't be is_ready. TODO: think about using a
        # single field with more states.
        # Also, if significance is trivial, it shouldn't be translated.
        return (self.is_approved and
                self.significance > TYPO_SIGNIFICANCE and
                self.document.locale == settings.WIKI_DEFAULT_LANGUAGE)


class HelpfulVote(ModelBase):
    """Helpful or Not Helpful vote on Revision."""
    revision = models.ForeignKey(Revision, related_name='poll_votes')
    helpful = models.BooleanField(default=False)
    created = models.DateTimeField(default=datetime.now, db_index=True)
    creator = models.ForeignKey(User, related_name='poll_votes', null=True)
    anonymous_id = models.CharField(max_length=40, db_index=True)
    user_agent = models.CharField(max_length=1000)

    def add_metadata(self, key, value):
        HelpfulVoteMetadata.objects.create(vote=self, key=key, value=value)


class HelpfulVoteMetadata(ModelBase):
    """Metadata for article votes."""
    vote = models.ForeignKey(HelpfulVote)
    key = models.CharField(max_length=40, db_index=True)
    value = models.CharField(max_length=1000)


class ImportantDate(ModelBase):
    """Important date that shows up globally on metrics graphs."""
    text = models.CharField(max_length=100)
    date = models.DateField(db_index=True)


class RelatedDocument(ModelBase):
    document = models.ForeignKey(Document, related_name='related_from')
    related = models.ForeignKey(Document, related_name='related_to')
    in_common = models.IntegerField()

    class Meta(object):
        ordering = ['-in_common']


def _doc_components_from_url(url, required_locale=None, check_host=True):
    """Return (locale, path, slug) if URL is a Document, False otherwise.

    If URL doesn't even point to the document view, raise _NotDocumentView.

    """
    # Extract locale and path from URL:
    parsed = urlparse(url)  # Never has errors AFAICT
    if check_host and parsed.netloc:
        return False
    locale, path = split_path(parsed.path)
    if required_locale and locale != required_locale:
        return False
    path = '/' + path

    try:
        view, view_args, view_kwargs = resolve(path)
    except Http404:
        return False

    import wiki.views  # Views import models; models import views.
    if view != wiki.views.document:
        raise _NotDocumentView
    return locale, path, view_kwargs['document_slug']


def points_to_document_view(url, required_locale=None):
    """Return whether a URL reverses to the document view.

    To limit the universe of discourse to a certain locale, pass in a
    `required_locale`.

    """
    try:
        return not not _doc_components_from_url(
            url, required_locale=required_locale)
    except _NotDocumentView:
        return False


# Default search parameters for the wiki:
wiki_search = (
    S(Document).filter(is_archived=False)
               .weight(title=6, content=1, keywords=4, summary=2))
# TODO: We probably have several more default filters to add.
