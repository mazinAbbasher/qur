from django import template

register = template.Library()

@register.inclusion_tag('sidebar.html', takes_context=True)
def sidebar(context):
	return {
		'request': context['request'],
		'active': context.get('active_sidebar', None),

    }