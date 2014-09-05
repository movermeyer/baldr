# coding=utf-8
from django.conf import settings
from django.conf.urls import url, include
from django.core.exceptions import PermissionDenied
from django.http import HttpResponse, Http404
from django.views.decorators.csrf import csrf_exempt
from odin.codecs import json_codec
from odin.exceptions import ValidationError
from baldr import content_type_resolvers
from baldr.exceptions import ImmediateErrorHttpResponse, ImmediateHttpResponse
from baldr.resources import Error, Listing


CODECS = {json_codec.CONTENT_TYPE: json_codec}
# Attempt to load other codecs that have dependencies
try:
    from odin.codecs import msgpack_codec
except ImportError:
    pass
else:
    CODECS[msgpack_codec.CONTENT_TYPE] = msgpack_codec


class ResourceApiBase(object):
    # The resource this API is modelled on.
    resource = None

    # Handlers used to resolve a content-type from a request.
    # These are checked in the order defined until one returns a content-type
    content_type_resolvers = [
        content_type_resolvers.accepts_header(),
        content_type_resolvers.content_type_header(),
        content_type_resolvers.settings_default(),
    ]

    # Codecs that are supported for Encoding/Decoding resources.
    registered_codecs = CODECS
    url_prefix = r''

    def __init__(self, api_name=None):
        if api_name:
            self.api_name = api_name
        elif not hasattr(self, 'api_name'):
            self.api_name = "%ss" % self.resource._meta.name

    def url(self, regex, view, kwargs=None, name=None, prefix=''):
        """
        Behaves like the django built in ``url`` method but constrains the URL to the API name.

        :param regex: This should be a part regex that applies only to the targeted method ie::

            self.url("(\d+)", ...)
        """
        if regex:
            return url(r'^%s/%s/?$' % (self.url_prefix + self.api_name.lower(), regex), view, kwargs, name, prefix)
        else:
            return url(r'^%s/?$' % (self.url_prefix + self.api_name.lower()), view, kwargs, name, prefix)

    @property
    def urls(self):
        """
        Return url conf for resource object.
        """
        return self.base_urls()

    def resolve_content_type(self, request):
        """
        Resolve the request content type from the request.

        :returns: Identified content type; or ``None`` if content type is not identified.

        """
        for resolver in self.content_type_resolvers:
            content_type = resolver(request)
            if content_type:
                return content_type

    def handle_500(self, request, exception):
        """
        Handle *un-handled* exceptions

        :param request: The request object.
        :param exception: The exception that was un-handled.
        :return: An ``HttpError`` response resource.

        """
        # This is an unknown exception, return an unknown error message.
        if settings.DEBUG:
            # If we are in debug mode return more details and the stack-trace.
            import sys
            import traceback

            the_trace = '\n'.join(traceback.format_exception(*(sys.exc_info())))
            return Error(500, 50000, "An unknown error has occurred, the developers have been notified.",
                         str(exception), the_trace)
        else:
            return Error(500, 50000, "An unknown error has occurred, the developers have been notified.")

    def wrap_view(self, view):
        """
        This method provides the main entry point for URL mappings in the ``base_urls`` method.
        """
        @csrf_exempt
        def wrapper(request, *args, **kwargs):
            # Resolve content type used to encode/decode request/response content.
            content_type = self.resolve_content_type(request)
            try:
                request.codec = codec = self.registered_codecs[content_type]
            except KeyError:
                # This is just a plain HTTP response, we can't provide a rich response when the content type is unknown
                return HttpResponse(content="Content cannot be returned in the format requested.", status=406)

            callback = getattr(self, view)
            try:
                result = callback(request, *args, **kwargs)
            except Http404 as e:
                # Item is not found.
                status = 404
                resource = Error(status, 40400, str(e))
            except ImmediateHttpResponse as e:
                # An exception used to return a response immediately, skipping any further processing.
                response = HttpResponse(codec.dumps(e.resource), content_type=codec.CONTENT_TYPE, status=e.status)
                for key, value in (e.headers or {}).items():
                    response[key] = value
                return response
            except ValidationError as e:
                # Validation of a resource has failed.
                status = 400
                if hasattr(e, 'message_dict'):
                    resource = Error(status, 40000, "Fields failed validation.", meta=e.message_dict)
                else:
                    resource = Error(status, 40000, str(e))
            except PermissionDenied as e:
                status = 403
                resource = Error(status, 40300, "Permission denied", str(e))
            except NotImplementedError:
                # A mixin method has not been implemented, as defining a mixing is explicit this is considered a server
                # error that should be addressed.
                status = 501
                resource = Error(status, 50100, "This method has not been implemented.")
            except Exception as e:
                # Catch any other exceptions and pass them to the 500 handler for evaluation.
                resource = self.handle_500(request, e)
                status = resource.status
            else:
                if isinstance(result, tuple) and len(result) == 2:
                    resource, status = result
                else:
                    resource = result
                    status = 204 if result is None else 200  # Return 204 (No Content) if result is None.
            if resource is None:
                return HttpResponse(status=status)
            else:
                return HttpResponse(codec.dumps(resource), content_type=codec.CONTENT_TYPE, status=status)
        return wrapper

    def base_urls(self):
        """
        Base URL mappings for this API.
        """
        return []

    def dispatch(self, request, request_type, **kwargs):
        """
        Primary method used to dispatch incoming requests to the appropriate method.
        """
        allowed_methods = getattr(self, "%s_allowed_methods" % request_type, [])
        request_method = self.method_check(request, allowed_methods)

        request.type = request_type

        self.handle_authorisation(request)

        method = getattr(self, "%s_%s" % (request_method, request_type), None)
        if method is None:
            raise NotImplementedError()

        # Allow for a pre_dispatch hook, a response from pre_dispatch would indicate an override of kwargs
        if hasattr(self, 'pre_dispatch'):
            response = self.pre_dispatch(request, **kwargs)
            if response is not None:
                kwargs = response

        result = method(request, **kwargs)

        # Allow for a post_dispatch hook, the response of which is returned
        if hasattr(self, 'post_dispatch'):
            return self.post_dispatch(request, result)
        else:
            return result

    def method_check(self, request, allowed):
        request_method = request.method.lower()

        if allowed is None:
            raise Http404('`%s` not found.' % self.api_name)

        if request_method not in allowed:
            raise ImmediateErrorHttpResponse(405, 40500, "Method not allowed", headers={
                'Allow': ','.join(map(str.upper, allowed))
            })
        return request_method

    def handle_authorisation(self, request):
        """
        Evaluate if a request is authorised.

        :param request: The current Django request object.

        """
        pass


class ResourceApi(ResourceApiBase):
    """
    Provides an API that returns a specified resource object.
    """
    list_allowed_methods = ['get']
    detail_allowed_methods = ['get']
    resource_id_regex = r'\d+'

    def base_urls(self):
        return super(ResourceApi, self).base_urls() + [
            # List URL
            self.url(
                r'',
                self.wrap_view('dispatch_list')
            ),
            # Detail URL
            self.url(
                r'(?P<resource_id>%s)' % self.resource_id_regex,
                self.wrap_view('dispatch_detail')
            )
        ]

    def dispatch_list(self, request, **kwargs):
        return self.dispatch(request, 'list', **kwargs)

    def dispatch_detail(self, request, **kwargs):
        return self.dispatch(request, 'detail', **kwargs)


class ActionMixin(ResourceApi):
    """
    Mixin to the resource API to provide support for sub resources, actions, aggregations.

    To hook up a action mixin specify a method that matches the type of request you want to handle ie::

        def get_summary_action(self, request, resource_id):
            pass

    """
    def base_urls(self):
        return super(ResourceApi, self).base_urls() + [
            # List Action URL
            self.url(
                r'(?P<action>[-\w\d]+)',
                self.wrap_view('dispatch_list_action')
            ),
            # Detail Action URL
            self.url(
                r'(?P<resource_id>%s)/(?P<action>[-\w\d]+)' % self.resource_id_regex,
                self.wrap_view('dispatch_detail_action')
            ),
        ]

    def dispatch_list_action(self, request, action, **kwargs):
        return self.dispatch(request, "%s_list" % action, **kwargs)

    def dispatch_detail_action(self, request, action, **kwargs):
        return self.dispatch(request, "%s_detail" % action, **kwargs)


class ListMixin(ResourceApi):
    """
    Mixin to the resource API that provides a nice listing API.
    """
    def get_list(self, request):
        offset = int(request.GET.get('offset', 0))
        limit = int(request.GET.get('limit', 50))
        result = self.list_resources(request, offset, limit)
        return Listing([r for r in result], limit, offset)

    def list_resources(self, request, offset, limit):
        """
        Load resources

        :param limit: Resource count limit.
        :param offset: Offset within the list to return.
        :return: List of resource objects.

        """
        raise NotImplementedError


class CreateMixin(ResourceApi):
    """
    Mixin to the resource API to provide a Create API.
    """
    def __init__(self, *args, **kwargs):
        super(CreateMixin, self).__init__(*args, **kwargs)
        self.list_allowed_methods.append('post')

    def post_list(self, request):
        resource = request.codec.loads(request.data, resource=self.resource)
        result = self.create_resource(request, resource, False)
        if result is None:
            return

    def put_list(self, request):
        resource = request.codec.loads(request.data, resource=self.resource)
        return self.create_resource(request, resource, True)

    def create_resource(self, request, resource, is_complete):
        """
        Create method.

        :param request: Django HttpRequest object.
        :param resource: The resource included with the request.
        :param is_complete: This is a complete resource (ie a PUT method).

        """
        raise NotImplementedError


class RetrieveMixin(ResourceApi):
    """
    Mixin to the resource API to provide a Retrieve API.
    """
    def get_detail(self, request, resource_id):
        return self.retrieve_resource(request, resource_id)

    def retrieve_resource(self, request, resource_id):
        """
        Retrieve method

        :param request: Django HttpRequest object.
        :param resource_id: The ID of the resource to retrieve.

        """
        raise NotImplementedError


class UpdateMixin(ResourceApi):
    """
    Mixin to the resource API to provide a Update API.
    """
    def __init__(self, *args, **kwargs):
        super(UpdateMixin, self).__init__(*args, **kwargs)
        self.detail_allowed_methods.append('post')

    def post_list(self, request, resource_id):
        resource = request.codec.loads(request.data, resource=self.resource)
        return self.update_resource(request, resource_id, resource, False)

    def put_list(self, request, resource_id):
        resource = request.codec.loads(request.data, resource=self.resource)
        return self.update_resource(request, resource_id, resource, True)

    def update_resource(self, request, resource_id, resource, is_complete):
        """
        Update method.

        :param request: Django HttpRequest object.
        :param resource_id: The ID of the resource to update.
        :param resource: The resource included with the request.
        :param is_complete: This is a complete resource (ie a PUT method).

        """
        raise NotImplementedError


class DeleteMixin(ResourceApi):
    """
    Mixin to the resource API to provide a Delete API.
    """
    def __init__(self, *args, **kwargs):
        super(DeleteMixin, self).__init__(*args, **kwargs)
        self.detail_allowed_methods.append('delete')

    def delete_detail(self, request, resource_id):
        return self.delete_resource(request, resource_id)

    def delete_resource(self, request, resource_id):
        """
        Delete method

        :param request: Django HttpRequest object.
        :param resource_id: The ID of the resource to delete.

        """
        raise NotImplementedError


class ApiCollection(object):
    """
    Collection of several resource API's.

    Along with helper methods for building URL patterns.
    """
    def __init__(self, *resource_apis, **kwargs):
        self.api_name = kwargs.pop('api_name', 'api')
        self.resource_apis = resource_apis

    @property
    def urls(self):
        if not hasattr(self, '_urls'):
            urls = []
            for resource_api in self.resource_apis:
                urls.extend(resource_api.urls)
            self._urls = urls
        return self._urls

    def include(self, namespace=None):
        return include(self.urls, namespace)

    def patterns(self):
        return [url(r'^%s/' % self.api_name, self.include())]
