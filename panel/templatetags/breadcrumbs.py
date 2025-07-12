from django import template

register = template.Library()

@register.inclusion_tag('breadcrumbs.html')
def show_breadcrumbs(breadcrumbs):
    return {'breadcrumbs': breadcrumbs}